"""Authenticated loopback controller client for the ephemeral relay."""

from __future__ import annotations

import json
import http.client
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable
from urllib.parse import urlsplit

from hephestus.relay.auth import sign_request
from hephestus.relay.crypto import EnvelopeError, derive_keys, open_envelope, seal
from hephestus.relay.limits import MAX_OUTPUT_CHARS, MAX_REQUEST_BYTES


class RelayHttpError(RuntimeError):
    def __init__(self, status: int, code: str = "relay_error") -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _failure(command: str, message: str, exit_code: int = -1) -> dict:
    return {
        "command": command,
        "stdout": "",
        "stderr": message,
        "exit_code": exit_code,
        "executed": False,
    }


class RelayControllerClient:
    def __init__(
        self,
        url: str,
        master_key: bytes,
        *,
        request_timeout: float = 10,
        result_timeout: float = 150,
        result_poll_interval: float = 0.5,
        job_ttl: int = 240,
        max_output_chars: int = 32_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        parsed = urlsplit(url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (parsed.scheme != "https" and not loopback)
        ):
            raise ValueError("Relay URL must be HTTPS or loopback HTTP without a path")
        if len(master_key) != 32:
            raise ValueError("Master key must contain exactly 32 bytes")
        if request_timeout <= 0 or result_timeout <= 0 or result_poll_interval <= 0:
            raise ValueError("Relay timeouts must be positive")
        if not 1 <= job_ttl <= 600:
            raise ValueError("Relay job TTL must be 1..600 seconds")
        if type(max_output_chars) is not int or not 1 <= max_output_chars <= MAX_OUTPUT_CHARS:
            raise ValueError(f"Relay output limit must be 1..{MAX_OUTPUT_CHARS} characters")
        self.url = url.rstrip("/")
        self.master_key = master_key
        self.auth_key = derive_keys(master_key).relay_auth
        self.request_timeout = request_timeout
        self.result_timeout = result_timeout
        self.result_poll_interval = result_poll_interval
        self.job_ttl = job_ttl
        self.max_output_chars = max_output_chars
        self.clock = clock

    def _request(self, method: str, path: str, payload: dict | None = None):
        body = b"" if payload is None else json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        headers = sign_request(self.auth_key, method, path, body)
        if body:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.url + path,
            data=body if method != "GET" else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                raw = response.read(MAX_REQUEST_BYTES + 1)
                if len(raw) > MAX_REQUEST_BYTES:
                    raise ValueError("Relay response exceeds protocol limit")
                try:
                    value = json.loads(raw.decode("utf-8")) if raw else None
                except RecursionError as exc:
                    raise ValueError("Invalid relay response") from exc
                if value is not None and not isinstance(value, dict):
                    raise ValueError("Relay response must be a JSON object")
                return value
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(MAX_REQUEST_BYTES + 1)
            finally:
                exc.close()
            code = "relay_error"
            if raw and len(raw) <= MAX_REQUEST_BYTES:
                try:
                    value = json.loads(raw.decode("utf-8"))
                    if isinstance(value, dict) and isinstance(value.get("error"), str):
                        code = value["error"]
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    pass
            raise RelayHttpError(exc.code, code) from exc

    def _unknown(self, command: str, job_id: str, message: str, exit_code: int = -1) -> dict:
        # Dispatch may already have reached the worker. Never label this safe to
        # retry, and attempt cancellation even if the result connection failed.
        try:
            self.disconnect()
        except (RelayHttpError, OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
            pass
        result = _failure(command, message, exit_code)
        result.update(executed=None, execution_uncertain=True, job_id=job_id)
        return result

    def execute_command(
        self,
        command: str,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict:
        if cancelled():
            return _failure(command, "Stopped before dispatch", 130)
        job_id = uuid.uuid4().hex
        now = int(self.clock())
        envelope = seal(
            self.master_key,
            {"kind": "command", "command": command},
            job_id=job_id,
            expires_at=now + self.job_ttl,
            now=now,
        )
        try:
            self._request("POST", "/v1/jobs", envelope)
        except RelayHttpError as exc:
            if exc.status >= 500:
                return self._unknown(command, job_id, f"Relay command acceptance is unknown ({exc.code})")
            if exc.code == "worker_offline":
                return _failure(command, "Remote worker is offline")
            if exc.code == "relay_busy":
                return _failure(command, "Relay already has an in-flight command")
            return _failure(command, f"Relay rejected the command ({exc.code})")
        except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError) as exc:
            return self._unknown(command, job_id, f"Relay command acceptance is unknown: {exc}")

        deadline = time.monotonic() + self.result_timeout
        while time.monotonic() < deadline:
            if cancelled():
                return self._unknown(command, job_id, "Stopped while waiting; remote execution outcome is unknown", 130)
            try:
                result_envelope = self._request("GET", f"/v1/jobs/{job_id}/result")
            except (RelayHttpError, OSError, ValueError, http.client.HTTPException, urllib.error.URLError) as exc:
                return self._unknown(command, job_id, f"Remote execution outcome is unknown; relay result request failed: {exc}")
            if result_envelope is None:
                time.sleep(self.result_poll_interval)
                continue
            try:
                payload = open_envelope(
                    self.master_key,
                    result_envelope,
                    expected_job_id=job_id,
                )
                return self._validate_result(command, payload)
            except (EnvelopeError, ValueError, TypeError):
                return self._unknown(command, job_id, "Remote execution outcome is unknown; invalid encrypted result")
        return self._unknown(command, job_id, "Timed out waiting; remote execution outcome is unknown", 124)

    def disconnect(self):
        self._request("POST", "/v1/session/stop")

    def _validate_result(self, command: str, payload: dict) -> dict:
        status = payload.get("status")
        executed = payload.get("executed")
        exit_code = payload.get("exit_code")
        stdout = payload.get("stdout")
        stderr = payload.get("stderr")
        if (
            status not in {"approved", "rejected", "quit", "update", "error"}
            or type(executed) is not bool
            or type(exit_code) is not int
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or len(stdout) + len(stderr) > self.max_output_chars
            or (status == "approved" and not executed)
            or (status in {"rejected", "quit", "update"} and executed)
        ):
            raise ValueError("Invalid result payload")
        result = {
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "executed": executed,
            "remote_status": status,
        }
        if status == "update":
            # The operator's surgical edit instruction, kept short and safe.
            instruction = payload.get("instruction")
            result["edit_instruction"] = instruction[:1000] if isinstance(instruction, str) else ""
        return result

