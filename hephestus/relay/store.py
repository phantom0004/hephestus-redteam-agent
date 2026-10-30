"""Strictly bounded, process-memory-only relay state."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from hephestus.relay.limits import MAX_ENVELOPE_CHARS

_JOB_ID = re.compile(r"^[a-f0-9]{32}$")
_FIELDS = {"v", "job_id", "expires_at", "iv", "ciphertext", "tag"}


class RelayStateError(RuntimeError):
    pass


@dataclass
class _ActiveJob:
    job_id: str
    expires_at: int
    command: dict | None
    leased: bool = False
    result: dict | None = None


def validate_opaque_envelope(
    envelope: object,
    *,
    now: int,
    expected_job_id: str | None = None,
    max_lifetime: int = 600,
) -> tuple[str, int]:
    if not isinstance(envelope, dict) or set(envelope) != _FIELDS:
        raise RelayStateError("invalid_envelope")
    job_id = envelope.get("job_id")
    expires_at = envelope.get("expires_at")
    encoded = (envelope.get("iv"), envelope.get("ciphertext"), envelope.get("tag"))
    if (
        envelope.get("v") != 1
        or not isinstance(job_id, str)
        or not _JOB_ID.fullmatch(job_id)
        or (expected_job_id is not None and job_id != expected_job_id)
        or type(expires_at) is not int
        or expires_at <= now
        or expires_at > now + max_lifetime
        or any(not isinstance(value, str) or not value or len(value) > MAX_ENVELOPE_CHARS for value in encoded)
    ):
        raise RelayStateError("invalid_envelope")
    return job_id, expires_at


class VolatileRelayStore:
    """One worker, one in-flight job, and no persistence or history."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        worker_timeout: int = 8,
        max_envelope_lifetime: int = 600,
    ) -> None:
        self._clock = clock
        self._worker_timeout = worker_timeout
        self._max_envelope_lifetime = max_envelope_lifetime
        self._last_worker_seen: int | None = None
        self._job: _ActiveJob | None = None
        self._condition = threading.Condition()
        self._disconnected = False

    def _now(self) -> int:
        return int(self._clock())

    def _cleanup_locked(self, now: int) -> None:
        if self._job is not None and self._job.expires_at <= now:
            self._job = None

    def heartbeat(self) -> None:
        with self._condition:
            self._disconnected = False
            self._last_worker_seen = self._now()
            self._condition.notify_all()

    def worker_online(self) -> bool:
        with self._condition:
            now = self._now()
            return (
                not self._disconnected and self._last_worker_seen is not None
                and now - self._last_worker_seen <= self._worker_timeout
            )

    def submit(self, envelope: dict) -> str:
        with self._condition:
            now = self._now()
            self._cleanup_locked(now)
            if (
                self._last_worker_seen is None
                or now - self._last_worker_seen > self._worker_timeout
            ):
                raise RelayStateError("worker_offline")
            if self._job is not None:
                raise RelayStateError("relay_busy")
            job_id, expires_at = validate_opaque_envelope(
                envelope,
                now=now,
                max_lifetime=self._max_envelope_lifetime,
            )
            self._job = _ActiveJob(job_id, expires_at, envelope.copy())
            self._condition.notify_all()
            return job_id

    def take_next(self, wait_seconds: float) -> dict | None:
        wait_seconds = max(0.0, wait_seconds)
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            self._last_worker_seen = self._now()
            while True:
                if self._disconnected:
                    raise RelayStateError("session_stopped")
                now = self._now()
                self._cleanup_locked(now)
                if self._job is not None and not self._job.leased and self._job.command:
                    envelope = self._job.command
                    self._job.command = None
                    self._job.leased = True
                    return envelope
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def post_result(self, job_id: str, envelope: dict) -> None:
        with self._condition:
            now = self._now()
            self._cleanup_locked(now)
            if (
                self._job is None
                or self._job.job_id != job_id
                or not self._job.leased
                or self._job.result is not None
            ):
                raise RelayStateError("unknown_job")
            validate_opaque_envelope(
                envelope,
                now=now,
                expected_job_id=job_id,
                max_lifetime=self._max_envelope_lifetime,
            )
            self._job.result = envelope.copy()
            self._condition.notify_all()

    def take_result(self, job_id: str) -> dict | None:
        with self._condition:
            now = self._now()
            self._cleanup_locked(now)
            if self._job is None or self._job.job_id != job_id or self._job.result is None:
                return None
            result = self._job.result
            self._job = None
            self._condition.notify_all()
            return result

    def disconnect(self):
        with self._condition:
            self._disconnected = True
            self._job = None
            self._last_worker_seen = None
            self._condition.notify_all()

    def job_active(self, job_id):
        with self._condition:
            self._cleanup_locked(self._now())
            return not self._disconnected and self._job is not None and self._job.job_id == job_id

