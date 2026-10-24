import json
import os
import platform
import re
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from hephestus import config

SENSITIVE_KEYS = {
    "key",
    "token",
    "secret",
    "pass",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
}

# Global callbacks for log events - allows external systems (like web UI) to receive logs
_log_callbacks: list[Callable[[dict], None]] = []


def add_log_callback(callback: Callable[[dict], None]) -> None:
    """Register a callback to receive log events."""
    _log_callbacks.append(callback)


def remove_log_callback(callback: Callable[[dict], None]) -> None:
    """Remove a previously registered callback."""
    if callback in _log_callbacks:
        _log_callbacks.remove(callback)


def clear_log_callbacks() -> None:
    """Remove all registered callbacks."""
    _log_callbacks.clear()


class Logger:
    def __init__(self, run_id: str = None) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(config.LOG_DIR, f"run_{ts}_{self.run_id[:8]}.jsonl")

        # Build a set of sensitive strings to redact from log output.
        # We scan environment variables whose names contain key/token/pass/secret
        # and collect their values (at least four characters to limit false positives).
        # These values will be replaced with [REDACTED] in all logged payloads.
        self.sensitive_strings = set()
        for k, v in os.environ.items():
            if len(v) >= 4 and any(
                s in k.lower() for s in ["key", "token", "pass", "secret"]
            ):
                self.sensitive_strings.add(v)

        # Log environment info at construction
        self._log_environment()

    def _log_environment(self) -> None:
        """Log environment information at run start."""
        env_info = {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "python_version": sys.version,
            "model_file": os.path.basename(config.LLM_MODEL)
            if config.LLM_MODEL
            else "",
        }
        self.log("environment_info", env_info)

    def _redact_string(self, value: str) -> str:
        for secret in self.sensitive_strings:
            if secret in value:
                value = value.replace(secret, "[REDACTED]")

        value = re.sub(
            r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)",
            r"\1=[REDACTED]",
            value,
        )
        value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@", r"\1[REDACTED]@", value)
        return value

    def _is_sensitive_key(self, key: str) -> bool:
        lowered = key.lower()
        return lowered in SENSITIVE_KEYS or lowered.endswith(
            ("_password", "_secret", "_api_key", "_access_token")
        )

    def _redact(self, obj: Any) -> Any:
        """Recursively walk a payload and redact sensitive data.

        Two redaction strategies:
          1. Key-based: if a dict key name contains "key", "token", "password"
             etc., its entire value is replaced with [REDACTED].
          2. Value-based: environment variable values collected at init time
             are string-replaced wherever they appear in log strings.
        """
        if isinstance(obj, str):
            return self._redact_string(obj)
        elif isinstance(obj, dict):
            redacted: dict[str, Any] = {}
            for k, v in obj.items():
                if self._is_sensitive_key(str(k)):
                    redacted[k] = "[REDACTED]"
                else:
                    redacted[k] = self._redact(v)
            return redacted
        elif isinstance(obj, list):
            return [self._redact(x) for x in obj]
        return obj

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event_type,
            "data": self._redact(payload),
            "dry_run": config.DRY_RUN,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        # Notify all registered callbacks
        for callback in list(_log_callbacks):
            try:
                callback(entry)
            except Exception:
                pass  # Don't let callback errors break logging
