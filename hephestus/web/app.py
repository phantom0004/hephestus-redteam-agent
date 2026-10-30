"""Loopback-only operator interface authenticated by a per-launch capability URL."""

import hmac
import ipaddress
import json
import queue
import secrets
import time
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_file, send_from_directory, session
from werkzeug.exceptions import HTTPException

from hephestus import config
from hephestus.src import local_llm, model_library, planner, telemetry
from hephestus.web.runner import get_runner

JSON_BODY_LIMIT = 32768
app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=secrets.token_hex(32),
    # Only model cover uploads may exceed the small JSON limit (checked below).
    MAX_CONTENT_LENGTH=model_library.MAX_COVER_BYTES + 4096,
    TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"],
    SESSION_COOKIE_NAME="hephestus_operator",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
)
ACCESS_TOKEN = secrets.token_urlsafe(32)
_model_operation = threading.Lock()


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
    if request.path == "/" or request.path.startswith("/assets/"):
        return None
    if (request.content_length or 0) > JSON_BODY_LIMIT and request.path != "/api/models/cover":
        return jsonify(error="Request body is too large"), 413
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
    if not hmac.compare_digest(supplied, ACCESS_TOKEN):
        return jsonify(error="Missing or invalid operator token"), 403


@app.before_request
def reserve_model_operation():
    from flask import g
    if request.method == "POST" and request.path in {
        "/api/start", "/api/ask", "/api/ask/stream", "/api/settings", "/api/settings/reset", "/api/thinking-level",
        "/api/models/load", "/api/models/unload", "/api/edit", "/api/connection",
    }:
        if not _model_operation.acquire(blocking=False):
            return jsonify(error="The model is busy; wait for the current operation"), 409
        g.model_reserved = True


@app.teardown_request
def release_model_operation(_error):
    from flask import g
    if g.pop("model_reserved", False):
        _model_operation.release()


def disconnect_worker():
    if config.BACKEND != "relay":
        return True
    store = app.extensions.get("relay_store")
    if store is not None:
        store.disconnect()
        return True
    try:
        from hephestus.src.executor import RelayBackend
        RelayBackend(request_timeout=3)._client.disconnect()
        return True
    except Exception:
        return False


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
    html = html.replace(
        "</head>",
        '<link rel="icon" type="image/png" href="/assets/logo.png">'
        '<link rel="stylesheet" href="/assets/workspace.css"></head>',
    )
    html = html.replace("<script>", f'<script nonce="{nonce}">')
    html = html.replace(
        "</body>",
        f'<script nonce="{nonce}" src="/assets/live_state.js"></script>'
        f'<script nonce="{nonce}" src="/assets/workspace.js"></script></body>',
    )
    response = Response(html, mimetype="text/html")
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}' 'self'; style-src 'unsafe-inline' 'self'; "
        "connect-src 'self'; img-src 'self' data: blob:; font-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    return response


@app.route("/assets/<path:filename>")
def assets(filename):
    if filename not in {"workspace.css", "workspace.js", "live_state.js", "logo.png", "brand-mark.png"}:
        return jsonify(error="Not found"), 404
    return send_from_directory(Path(__file__).parent / "static", filename)


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
                "llm_batch_size",
                "thinking_level",
                "command_timeout_seconds",
            }
        }
    )


def runner_is_active():
    return get_runner().get_state().get("status") in {
        "running",
        "waiting_for_user",
        "stopping",
    }


def remote_session():
    from hephestus.web.remote_session import RemoteSession
    if 'remote_session' not in app.extensions:
        app.extensions['remote_session'] = RemoteSession(app)
    return app.extensions['remote_session']


@app.route('/api/connection', methods=['GET', 'POST'])
def connection():
    if request.method == 'GET':
        return jsonify(remote_session().status())
    if runner_is_active():
        return jsonify(error='Stop the current task before switching execution mode.'), 409
    mode = text_field(json_object(), 'mode', 16)
    if mode not in {'local', 'remote'}:
        raise ValueError('mode must be local or remote')
    remote = remote_session()
    if mode == 'local':
        remote.close()
        config.BACKEND = 'local'
    else:
        try:
            remote.start()
        except (OSError, RuntimeError, SystemExit) as exc:
            config.BACKEND = 'local'
            return jsonify(error=str(exc) or 'Remote connection could not start. Local mode remains available.'), 503
        config.BACKEND = 'relay'
    return jsonify(remote.status())


@app.route('/api/connection/bundle')
def connection_bundle():
    from io import BytesIO
    # Serialize download with switching so keys cannot change mid-archive.
    with _model_operation:
        try:
            bundle = remote_session().bundle()
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
    return send_file(BytesIO(bundle), mimetype='application/zip', as_attachment=True,
                     download_name='remote-connection.zip')


def _gpu_info():
    try:
        layers, info = local_llm.suggest_gpu_layers()
        return {"layers": layers, **info}
    except Exception:
        return {"available": False}


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "GET":
        data = config.runtime_settings()
        data["gpu_suggestion"] = _gpu_info()
        data["gpu_effective"] = local_llm.effective_gpu_layers()
        return jsonify(data)
    if runner_is_active():
        return jsonify(error="Stop the current task before changing model settings"), 409
    data = json_object()
    previous = config.runtime_settings()
    try:
        settings = config.apply_runtime_overrides(data)
        loader_changed = any(
            name in data
            and config.RUNTIME_CONTROL_ALIASES[name] in config.LOADER_SETTINGS
            and previous[name] != settings[name]
            for name in config.RUNTIME_CONTROL_ALIASES
        )
        if loader_changed:
            local_llm.reload_model()
    except ValueError as exc:
        config.restore_runtime_settings(previous)
        return jsonify(error=str(exc)), 400
    except Exception:
        config.restore_runtime_settings(previous)
        config.persist_runtime_settings()
        return jsonify(error="The model could not reload with those settings; the previous values remain active"), 503
    config.persist_runtime_settings()
    return jsonify(settings=settings, reloaded=loader_changed, gpu_suggestion=_gpu_info())


@app.route("/api/settings/reset", methods=["POST"])
def reset_settings():
    if runner_is_active():
        return jsonify(error="Stop the current task before changing model settings"), 409
    previous = config.runtime_settings()
    try:
        settings = config.reset_runtime_settings()
        loader_changed = any(
            config.RUNTIME_CONTROL_ALIASES[name] in config.LOADER_SETTINGS
            and previous[name] != settings[name]
            for name in config.RUNTIME_CONTROL_ALIASES
        )
        if loader_changed:
            local_llm.reload_model()
    except Exception:
        config.restore_runtime_settings(previous)
        config.persist_runtime_settings()
        return jsonify(error="The model could not reload with default settings; the previous values remain active"), 503
    return jsonify(settings=settings, reloaded=loader_changed, gpu_suggestion=_gpu_info())


@app.route("/api/thinking-level", methods=["POST"])
def thinking_level():
    if runner_is_active():
        return jsonify(error="Stop the current task before changing thinking level"), 409
    level = text_field(json_object(), "thinking_level", 16)
    settings = config.set_thinking_level(level)
    config.persist_runtime_settings()
    return jsonify(settings=settings, reloaded=False)


def _handoff_filename(snapshot, extension):
    run_id = (snapshot.get("session") or {}).get("run_id") or "session"
    short = "".join(c for c in str(run_id) if c.isalnum())[:12] or "session"
    return f"hephestus-handoff-{short}.{extension}"


@app.route("/api/export")
def export_run():
    run_id = request.args.get("run_id", "").strip() or None
    fmt = request.args.get("format", "json").strip().lower()
    if run_id is not None and len(run_id) > 64:
        return jsonify(error="Invalid run id"), 400
    if fmt not in {"json", "pdf"}:
        return jsonify(error="format must be json or pdf"), 400
    try:
        snapshot = get_runner().export_snapshot(run_id)
    except RuntimeError:
        return jsonify(error="Session not found"), 404
    if fmt == "pdf":
        try:
            from hephestus.web import report

            pdf = report.build_engagement_pdf(
                snapshot
            )
        except ImportError:
            return jsonify(
                error="PDF export needs the reportlab package (pip install reportlab)"
            ), 503
        except Exception:
            return jsonify(error="The PDF handoff could not be generated"), 500
        return Response(
            pdf,
            mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={_handoff_filename(snapshot, 'pdf')}"},
        )
    try:
        payload = json.dumps(snapshot, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return jsonify(error="The engagement handoff could not be generated"), 500
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={_handoff_filename(snapshot, 'json')}"},
    )


@app.route("/api/ask", methods=["POST"])
def ask_about_run():
    runner = get_runner()
    # Chat is allowed during ``waiting_for_user`` because the agent thread is
    # parked on an approval event and the inference process is idle. If the
    # operator approves mid-chat, the next planner call simply queues behind
    # the chat on InferenceProcess._calls — serialization stays safe.
    if runner.get_state().get("status") in {"running", "stopping"}:
        return jsonify(error="Wait for the active task before asking about its logs"), 409
    data = json_object()
    question = text_field(data, "question", 2000)
    run_id = text_field(data, "run_id", 64)
    if len(question) < 3:
        return jsonify(error="Question must be at least 3 characters"), 400
    if not local_llm.is_loaded():
        return jsonify(error="No model is loaded. Open Models and load one to ask questions."), 409
    started = time.monotonic()
    log_context = (
        runner.get_session_log_context(run_id, question)
        if run_id
        else runner.get_log_context(question)
    )
    if log_context is None:
        return jsonify(error="Session not found"), 404
    answer = planner.answer_logs(question, log_context)
    if isinstance(answer, dict):
        return jsonify(error=answer["error"]), answer.get("status", 503)
    if not answer:
        return jsonify(error="The local model could not answer from this session"), 503
    return jsonify(answer=answer, seconds=round(time.monotonic() - started, 1),
                   context_chars=len(log_context))


@app.route("/api/ask/stream", methods=["POST"])
def ask_about_run_stream():
    """Like /api/ask, but streams the answer as server-sent events while it is generated."""
    from flask import g
    runner = get_runner()
    # Streaming chat is allowed during ``waiting_for_user``: the agent thread
    # is blocked in ``_approval_event.wait`` and holds no inference lock, so
    # the GPU is free. Any subsequent planner call after the operator decides
    # queues behind the stream on InferenceProcess._calls.
    if runner.get_state().get("status") in {"running", "stopping"}:
        return jsonify(error="Wait for the active task before asking about its logs"), 409
    data = json_object()
    question = text_field(data, "question", 2000)
    run_id = text_field(data, "run_id", 64)
    if len(question) < 3:
        return jsonify(error="Question must be at least 3 characters"), 400
    if not local_llm.is_loaded():
        return jsonify(error="No model is loaded. Open Models and load one to ask questions."), 409
    started = time.monotonic()
    log_context = (
        runner.get_session_log_context(run_id, question)
        if run_id
        else runner.get_log_context(question)
    )
    if log_context is None:
        return jsonify(error="Session not found"), 404
    events = queue.Queue()
    # The generation thread now owns the model reservation and releases it when
    # the model is finished, even if the browser disconnects mid-answer.
    g.model_reserved = False

    def work():
        try:
            finish, text = planner.stream_answer_logs(question, log_context, lambda t: events.put(("delta", t)))
            events.put(("done", {"truncated": finish == "length", "empty": not text.strip(),
                                 "seconds": round(time.monotonic() - started, 1)}))
        except local_llm.ModelNotLoadedError as exc:
            events.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - details stay in the terminal
            print(f"Local log analyst error ({type(exc).__name__}): {exc}")
            events.put(("error", "The local model could not answer from this session"))
        finally:
            _model_operation.release()

    threading.Thread(target=work, daemon=True, name="hephestus-ask").start()

    def generate():
        yield f"data: {json.dumps({'type': 'meta', 'context_chars': len(log_context)})}\n\n"
        while True:
            kind, value = events.get()
            if kind == "delta":
                yield f"data: {json.dumps({'type': 'delta', 'text': value})}\n\n"
            elif kind == "done":
                yield f"data: {json.dumps({'type': 'done', **value})}\n\n"
                return
            else:
                yield f"data: {json.dumps({'type': 'error', 'error': value})}\n\n"
                return

    return Response(generate(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.route("/api/start", methods=["POST"])
def start_run():
    data = json_object()
    goal = text_field(data, "goal", 5000)
    context = text_field(data, "context", 10000)
    mode = text_field(data, "mode", 16) or "goal"
    if len(goal) < 5:
        return jsonify(error="Goal must be at least 5 characters"), 400
    if config.CONFIG_ERRORS:
        return jsonify(error="Invalid local configuration; check the terminal"), 400
    model = local_llm.model_state()
    if model["status"] != "loaded":
        message = ("The model is still loading; wait for it to finish." if model["status"] == "loading"
                   else "No model is loaded. Open Models and load one to start a task.")
        return jsonify(error=message), 409
    if mode not in {"goal", "quick"}:
        return jsonify(error="mode must be goal or quick"), 400
    if get_runner().start(goal, context, mode):
        return jsonify(status="started")
    return jsonify(error="A worker is still active or stopping"), 409


@app.route("/api/stop", methods=["POST"])
def stop_run():
    # The model stays loaded: the operator chooses when to unload it in Models.
    stopping = get_runner().stop()
    disconnected = disconnect_worker()
    return jsonify(status="stopping" if stopping else "stopped",
                   disconnected=disconnected,
                   warning=None if disconnected else "Relay unreachable; remote cancellation is not confirmed")


@app.route("/api/complete", methods=["POST"])
def complete_run():
    if get_runner().complete():
        return jsonify(status="completing")
    return jsonify(error="No active run"), 409


@app.route("/api/sessions")
def sessions():
    return jsonify(sessions=get_runner().get_sessions())


@app.route("/api/sessions/<run_id>")
def session_detail(run_id):
    if len(run_id) > 64:
        return jsonify(error="Invalid run id"), 400
    result = get_runner().get_session(run_id)
    if result is None:
        return jsonify(error="Session not found"), 404
    return jsonify(result)


@app.route("/api/sessions/<run_id>/rename", methods=["POST"])
def rename_session(run_id):
    if len(run_id) > 64:
        return jsonify(error="Invalid run id"), 400
    title = text_field(json_object(), "title", 120)
    if get_runner().set_session_title(run_id, title):
        return jsonify(status="renamed", title=title)
    return jsonify(error="Session not found"), 404


@app.route("/api/sessions/<run_id>", methods=["DELETE"])
def delete_session(run_id):
    if len(run_id) > 64:
        return jsonify(error="Invalid run id"), 400
    runner = get_runner()
    if run_id == runner.run_id and runner_is_active():
        runner.stop()
        disconnect_worker()
        runner.shutdown(unload=False)
    if runner.delete_session(run_id):
        return jsonify(status="deleted")
    return jsonify(
        error="Stop the active run before deleting it, or the session was not found"
    ), 409


@app.route("/api/killswitch", methods=["POST"])
def activate_killswitch():
    return stop_run()


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


@app.route("/api/edit", methods=["POST"])
def edit_pending_command():
    """Apply a surgical edit to one pending command while awaiting approval."""
    data = json_object()
    proposal = text_field(data, "proposal_id", 64)
    instruction = text_field(data, "instruction", 1000)
    index = data.get("index")
    if type(index) is not int or not 0 <= index < 100:
        return jsonify(error="index must be a valid command position"), 400
    if not local_llm.is_loaded():
        return jsonify(error="No model is loaded"), 409
    variant = get_runner().edit_pending(proposal, index, instruction)
    if variant is None:
        return jsonify(error="The command could not be edited (stale proposal or no valid result)."), 409
    return jsonify(status="edited", command=variant, proposal_id=get_runner().proposal_id)


@app.route("/api/state")
def get_state():
    state = get_runner().get_state()
    model = local_llm.model_state()
    state.update(
        backend=config.BACKEND,
        max_steps=config.MAX_STEPS,
        llm_reachable=model["status"] == "loaded",
        model=model,
    )
    return jsonify(state)


def sequence_cursor():
    value = request.args.get("since", "0")
    if not value.isdigit() or len(value) > 12:
        raise ValueError("since must be a non-negative integer sequence")
    return int(value)


@app.route("/api/events")
def events():
    since = sequence_cursor()
    runner = get_runner()
    state = runner.get_state()
    state.update(backend=config.BACKEND, max_steps=config.MAX_STEPS)
    return jsonify(state=state, logs=runner.get_logs(since), model=local_llm.model_state())


@app.route("/api/telemetry/gpu")
def gpu_telemetry():
    since = request.args.get("since", "0")
    if not since.isdigit() or len(since) > 15:
        return jsonify(error="since must be a millisecond timestamp"), 400
    return jsonify(telemetry.sampler.history(int(since)))


# ---------- model library ----------
@app.route("/api/models")
def list_models():
    return jsonify(models=model_library.scan(), state=local_llm.model_state(),
                   models_dir=str(model_library.models_dir()))


@app.route("/api/models/load", methods=["POST"])
def load_model():
    if runner_is_active():
        return jsonify(error="Stop the current task before switching models"), 409
    model_id = text_field(json_object(), "id", 400)
    try:
        path = model_library.resolve(model_id)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify(error=str(exc)), 404
    state = local_llm.model_state()
    if state["status"] in {"loading", "unloading"}:
        return jsonify(error="Another model operation is in progress"), 409
    if state["status"] == "loaded" and state["model_path"] and Path(state["model_path"]).resolve() == path:
        return jsonify(status="loaded", state=state)
    if state["status"] == "loaded":
        local_llm.unload_model()
    # Claim "loading" before returning so the UI never sees a stale "unloaded".
    if not local_llm.begin_load(path):
        return jsonify(error="Another model operation is in progress"), 409
    local_llm.load_model_async(path)
    return jsonify(status="loading", state=local_llm.model_state()), 202


@app.route("/api/models/unload", methods=["POST"])
def unload_model():
    if runner_is_active():
        return jsonify(error="Stop the current task before unloading the model"), 409
    if local_llm.model_state()["status"] == "loading":
        return jsonify(error="Wait for the model to finish loading"), 409
    local_llm.unload_model()
    return jsonify(status="unloaded", state=local_llm.model_state())


@app.route("/api/models/delete", methods=["POST"])
def delete_model():
    data = json_object()
    model_id = text_field(data, "id", 400)
    confirm = text_field(data, "confirm", 400)
    try:
        path = model_library.resolve(model_id)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify(error=str(exc)), 404
    if confirm != path.name:
        return jsonify(error="Type the exact file name to confirm deletion"), 400
    try:
        name = model_library.delete_model(model_id)
    except PermissionError as exc:
        return jsonify(error=str(exc)), 409
    except OSError:
        return jsonify(error="The model file could not be deleted (is another program using it?)"), 500
    return jsonify(status="deleted", file=name, models=model_library.scan())


@app.route("/api/models/cover", methods=["GET", "POST", "DELETE"])
def model_cover():
    model_id = request.args.get("id", "")
    try:
        path = model_library.resolve(model_id)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify(error=str(exc)), 404
    if request.method == "GET":
        cover = model_library.cover_path(path)
        if cover is None:
            return jsonify(error="No cover"), 404
        response = send_file(cover, mimetype=model_library.COVER_TYPES[cover.suffix.lower()])
        response.headers["Cache-Control"] = "private, max-age=60"
        return response
    if request.method == "DELETE":
        model_library.remove_cover(model_id)
        return jsonify(status="removed")
    try:
        model_library.save_cover(model_id, request.get_data(cache=False))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="saved")


@app.route("/api/stream")
def stream():
    initial_cursor = sequence_cursor()

    def generate():
        runner = get_runner()
        cursor = initial_cursor
        previous = None
        quiet = 0
        while True:
            state = runner.get_state()
            changed = False
            logs = runner.get_logs(cursor)
            for entry in logs:
                cursor = entry["sequence"]
                yield f"data: {json.dumps({'type': 'log', 'entry': entry}, default=str)}\n\n"
                changed = True
            identity = json.dumps(state, sort_keys=True, default=str)
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
