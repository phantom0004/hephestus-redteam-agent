"""HMAC request authentication with timestamp and in-memory replay checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Callable

TIME_HEADER = "X-Hephestus-Time"
NONCE_HEADER = "X-Hephestus-Nonce"
SIGNATURE_HEADER = "X-Hephestus-Signature"
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class RequestAuthError(ValueError):
    """Request authentication failed without exposing which check failed."""


def _canonical(method: str, path: str, body: bytes, timestamp: int, nonce: str) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{digest}".encode("utf-8")


def sign_request(
    auth_key: bytes,
    method: str,
    path: str,
    body: bytes,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    if not isinstance(auth_key, bytes) or len(auth_key) != 32:
        raise ValueError("Relay authentication key must contain exactly 32 bytes")
    if not isinstance(body, bytes) or not path.startswith("/"):
        raise ValueError("Invalid request signing input")
    value_time = int(time.time()) if timestamp is None else int(timestamp)
    value_nonce = nonce or secrets.token_urlsafe(18)
    if not _NONCE.fullmatch(value_nonce):
        raise ValueError("Invalid request nonce")
    signature = hmac.new(
        auth_key,
        _canonical(method, path, body, value_time, value_nonce),
        hashlib.sha256,
    ).digest()
    return {
        TIME_HEADER: str(value_time),
        NONCE_HEADER: value_nonce,
        SIGNATURE_HEADER: base64.b64encode(signature).decode("ascii"),
    }


class RequestVerifier:
    def __init__(
        self,
        auth_key: bytes,
        *,
        max_clock_skew: int = 30,
        nonce_ttl: int = 90,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(auth_key, bytes) or len(auth_key) != 32:
            raise ValueError("Relay authentication key must contain exactly 32 bytes")
        if max_clock_skew < 1 or nonce_ttl <= max_clock_skew:
            raise ValueError("Invalid authentication timing limits")
        self._auth_key = auth_key
        self._max_clock_skew = max_clock_skew
        self._nonce_ttl = nonce_ttl
        self._clock = clock
        self._nonces: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _headers(headers: Mapping[str, str]) -> dict[str, str]:
        return {str(key).lower(): str(value) for key, value in headers.items()}

    def verify(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> None:
        try:
            normalized = self._headers(headers)
            timestamp = int(normalized[TIME_HEADER.lower()])
            nonce = normalized[NONCE_HEADER.lower()]
            supplied = base64.b64decode(
                normalized[SIGNATURE_HEADER.lower()].encode("ascii"), validate=True
            )
            if not _NONCE.fullmatch(nonce) or len(supplied) != 32:
                raise ValueError
            now = int(self._clock())
            if abs(now - timestamp) > self._max_clock_skew:
                raise ValueError
            expected = hmac.new(
                self._auth_key,
                _canonical(method, path, body, timestamp, nonce),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied, expected):
                raise ValueError
            with self._lock:
                self._nonces = {
                    value: expiry for value, expiry in self._nonces.items() if expiry >= now
                }
                if nonce in self._nonces:
                    raise ValueError
                self._nonces[nonce] = now + self._nonce_ttl
        except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
            raise RequestAuthError("Request authentication failed") from exc

