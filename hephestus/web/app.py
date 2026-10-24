"""Loopback-only operator interface authenticated by a per-launch capability URL."""

import hmac
import ipaddress
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, session
from werkzeug.exceptions import HTTPException

from hephestus import config
from hephestus.src.policy_engine import KILL_SWITCH_FILENAME
from hephestus.utils.metrics import generate_summary
from hephestus.web.runner import get_runner

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=secrets.token_hex(32),
    MAX_CONTENT_LENGTH=32768,
    TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"],
    SESSION_COOKIE_NAME="hephestus_operator",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
)
ACCESS_TOKEN = secrets.token_urlsafe(32)


@app.before_request
def protect_operator():
    if urlsplit(request.host_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        return jsonify(error="Invalid host"), 400
    if request.remote_addr:
        try:
            if not ipaddress.ip_address(request.remote_addr).is_loopback:
                return jsonify(error="Only loopback clients are allowed"), 403
        except ValueError:
            return jsonify(error="Invalid client address"), 403
    if request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
        return jsonify(error="Cross-origin requests are denied"), 403
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return jsonify(error="Cross-origin requests are denied"), 403
    if request.path == "/":
        return None
    supplied = request.headers.get("X-Hephestus-Token", "")
    if request.path == "/api/session":
        if request.method != "POST" or not hmac.compare_digest(supplied, ACCESS_TOKEN):
            return jsonify(
                error="Open the capability URL printed by python web.py"
            ), 403
        session["operator"] = True
        return None
    if not session.get("operator"):
        return jsonify(error="Open the capability URL printed by python web.py"), 403
    if request.method != "GET" and not hmac.compare_digest(supplied, ACCESS_TOKEN):
        return jsonify(error="Missing or invalid operator token"), 403


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault(
        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
    )
    return response


@app.errorhandler(HTTPException)
def http_error(exc):
    return jsonify(error=exc.name), exc.code


def json_object():
    data = request.get_json()
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def text_field(data, key, limit):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{key} must be text of at most {limit} characters")
    return value.strip()


@app.errorhandler(ValueError)
def invalid_value(exc):
    return jsonify(error=str(exc)), 400


@app.route("/")
def index():
    nonce = secrets.token_urlsafe(24)
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("<script>", f'<script nonce="{nonce}">')
    response = Response(html, mimetype="text/html")
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    return response


@app.route("/api/session", methods=["POST"])
def authenticate():
    return jsonify(status="authenticated")


@app.route("/api/config")
def get_config():
    snapshot = config.snapshot()
    return jsonify(
        {
            k: v
            for k, v in snapshot.items()
            if k
            in {
                "backend",
                "dry_run",
                "max_steps",
                "max_tokens",
                "temperature",
                "top_p",
                "llm_context_size",
                "llm_gpu_layers",
                "llm_threads",
                "command_timeout_seconds",
            }
        }
    )


@app.route("/metrics/report")
def get_metrics_report():
    return jsonify(generate_summary(config.LOG_DIR))


@app.route("/api/start", methods=["POST"])
def start_run():
    data = json_object()
    goal = text_field(data, "goal", 5000)
    context = text_field(data, "context", 10000)
    if len(goal) < 5:
        return jsonify(error="Goal must be at least 5 characters"), 400
    if config.CONFIG_ERRORS:
        return jsonify(error="Invalid local configuration; check the terminal"), 400
    from hephestus.src.local_llm import model_is_available

    if not model_is_available():
        return jsonify(
            error="Model or runtime unavailable. Run python setup.py or configure LLM_MODEL."
        ), 400
    if get_runner().start(goal, context):
        return jsonify(status="started")
    return jsonify(error="A worker is still active or stopping"), 409


@app.route("/api/stop", methods=["POST"])
def stop_run():
    if get_runner().stop():
        return jsonify(status="stopping")
    return jsonify(error="No active run"), 409


@app.route("/api/killswitch", methods=["POST"])
def activate_killswitch():
    directory = Path(config.LOG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / KILL_SWITCH_FILENAME).write_text("operator stop", encoding="utf-8")
    get_runner().stop()
    return jsonify(status="kill_switch_activated")


@app.route("/api/approve", methods=["POST"])
def approve_commands():
    data = json_object()
    proposal = text_field(data, "proposal_id", 64)
    indices = data.get("approved", [])
    if (
        not isinstance(indices, list)
        or len(indices) > 100
        or any(type(i) is not int for i in indices)
    ):
        return jsonify(error="approved must be a list of integer indices"), 400
    for flag in ("abort", "reject_all", "confirmed_privilege"):
        if flag in data and type(data[flag]) is not bool:
            return jsonify(error=f"{flag} must be a boolean"), 400
    guidance = text_field(data, "update_goal", 10000)
    if (
        sum(
            [
                bool(data.get("abort")),
                bool(data.get("reject_all")),
                bool(guidance),
                bool(indices),
            ]
        )
        != 1
    ):
        return jsonify(error="Provide exactly one approval decision"), 400
    runner = get_runner()
    if data.get("abort"):
        accepted = runner.abort(proposal)
    elif data.get("reject_all"):
        accepted = runner.reject_all(proposal)
    elif guidance:
        accepted = runner.update_goal(guidance, proposal)
    else:
        accepted = runner.approve(
            indices, proposal, data.get("confirmed_privilege", False)
        )
    if accepted:
        return jsonify(status="accepted")
    return jsonify(
        error="Stale proposal, duplicate decision or missing privilege confirmation"
    ), 409


@app.route("/api/state")
def get_state():
    from hephestus.src.local_llm import model_is_available

    state = get_runner().get_state()
    state.update(
        backend=config.BACKEND,
        max_steps=config.MAX_STEPS,
        llm_reachable=model_is_available()
        if state["status"] in {"idle", "error", "completed"}
        else None,
    )
    return jsonify(state)


@app.route("/api/stream")
def stream():
    def generate():
        runner = get_runner()
        cursor = 0
        previous = None
        quiet = 0
        while True:
            state = runner.get_state()
            changed = False
            logs = runner.get_logs(cursor)
            for entry in logs:
                cursor = entry["sequence"]
                yield f"data: {json.dumps({'type': 'log', 'entry': entry})}\n\n"
                changed = True
            identity = (state["status"], state["proposal_id"], state["run_id"])
            if identity != previous:
                previous = identity
                yield f"data: {json.dumps({'type': 'status', **state})}\n\n"
                changed = True
            if state["status"] in {"completed", "error", "idle"} and not changed:
                quiet += 1
                if quiet >= 25:
                    return
            else:
                quiet = 0
            time.sleep(0.2)

    return Response(
        generate(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


def create_app():
    return app
