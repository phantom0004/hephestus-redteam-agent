"""Minimal authenticated Flask API for volatile encrypted message forwarding."""

from __future__ import annotations

import logging
import time
from typing import Callable

from flask import Flask, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from hephestus.relay.auth import RequestAuthError, RequestVerifier
from hephestus.relay.store import RelayStateError, VolatileRelayStore
from hephestus.relay.limits import MAX_REQUEST_BYTES


def create_relay_app(
    relay_auth_key: bytes,
    *,
    clock: Callable[[], float] = time.time,
    worker_timeout: int = 8,
    max_long_poll: int = 20,
) -> Flask:
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES, JSON_SORT_KEYS=True)
    app.logger.disabled = True
    logging.getLogger("werkzeug").disabled = True
    verifier = RequestVerifier(relay_auth_key, clock=clock)
    store = VolatileRelayStore(clock=clock, worker_timeout=worker_timeout)
    app.extensions["hephestus_relay_store"] = store

    @app.before_request
    def authenticate():
        if request.path == "/health":
            return None
        body = request.get_data(cache=True)
        target = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
        try:
            verifier.verify(request.method, target, body, request.headers)
        except RequestAuthError:
            return jsonify(error="unauthorized"), 401
        return None

    @app.after_request
    def secure_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return jsonify(error="request_too_large"), 413

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify(error="not_found"), 404

    @app.errorhandler(Exception)
    def internal_error(error):
        if isinstance(error, RequestEntityTooLarge):
            return jsonify(error="request_too_large"), 413
        return jsonify(error="internal_error"), 500

    @app.get("/health")
    def health():
        return jsonify(status="ready", storage="memory-only")

    @app.post("/v1/worker/heartbeat")
    def worker_heartbeat():
        store.heartbeat()
        return "", 204

    @app.post("/v1/jobs")
    def submit_job():
        envelope = request.get_json(silent=True)
        try:
            job_id = store.submit(envelope)
        except RelayStateError as exc:
            status = 409 if str(exc) in {"worker_offline", "relay_busy"} else 400
            return jsonify(error=str(exc)), status
        return jsonify(job_id=job_id), 202

    @app.get("/v1/jobs/next")
    def next_job():
        try:
            wait = float(request.args.get("wait", "0"))
        except ValueError:
            return jsonify(error="invalid_wait"), 400
        wait = min(max(0.0, wait), float(max_long_poll))
        try:
            envelope = store.take_next(wait)
        except RelayStateError:
            return jsonify(error="session_stopped"), 410
        return (jsonify(envelope), 200) if envelope is not None else ("", 204)

    @app.post("/v1/session/stop")
    def stop_session():
        store.disconnect()
        return "", 204

    @app.get("/v1/jobs/<job_id>/active")
    def job_active(job_id):
        return jsonify(active=store.job_active(job_id))

    @app.post("/v1/jobs/<job_id>/result")
    def post_result(job_id: str):
        envelope = request.get_json(silent=True)
        try:
            store.post_result(job_id, envelope)
        except RelayStateError as exc:
            status = 404 if str(exc) == "unknown_job" else 400
            return jsonify(error=str(exc)), status
        return "", 204

    @app.get("/v1/jobs/<job_id>/result")
    def get_result(job_id: str):
        envelope = store.take_result(job_id)
        return (jsonify(envelope), 200) if envelope is not None else ("", 204)

    return app

