"""PowerShell 5.1-compatible authenticated payload encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hephestus.relay.limits import MAX_ENVELOPE_CHARS

_JOB_ID = re.compile(r"^[a-f0-9]{32}$")
_ENVELOPE_FIELDS = {"v", "job_id", "expires_at", "iv", "ciphertext", "tag"}


class EnvelopeError(ValueError):
    """An encrypted envelope is invalid, unauthentic, or expired."""


@dataclass(frozen=True)
class DerivedKeys:
    encryption: bytes
    payload_mac: bytes
    relay_auth: bytes


def _derive(master_key: bytes, label: bytes) -> bytes:
    return hmac.new(master_key, b"hephestus-v1\x00" + label, hashlib.sha256).digest()


def derive_keys(master_key: bytes) -> DerivedKeys:
    if not isinstance(master_key, bytes) or len(master_key) != 32:
        raise ValueError("Master key must contain exactly 32 bytes")
    return DerivedKeys(
        encryption=_derive(master_key, b"payload-encryption"),
        payload_mac=_derive(master_key, b"payload-authentication"),
        relay_auth=_derive(master_key, b"relay-request-authentication"),
    )


def _canonical_envelope(envelope: dict) -> bytes:
    authenticated = {key: envelope[key] for key in sorted(_ENVELOPE_FIELDS - {"tag"})}
    return json.dumps(
        authenticated, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_ENVELOPE_CHARS:
        raise EnvelopeError("Invalid encrypted envelope")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise EnvelopeError("Invalid encrypted envelope") from exc


def seal(
    master_key: bytes,
    payload: dict,
    *,
    job_id: str,
    expires_at: int,
    now: int | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> dict:
    current = int(time.time()) if now is None else int(now)
    if not _JOB_ID.fullmatch(job_id or "") or type(expires_at) is not int:
        raise ValueError("Invalid job identifier or expiry")
    if expires_at <= current:
        raise ValueError("Envelope expiry must be in the future")
    if not isinstance(payload, dict):
        raise TypeError("Payload must be a JSON object")

    plaintext = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    iv = random_bytes(16)
    if len(iv) != 16:
        raise ValueError("IV source must return 16 bytes")
    keys = derive_keys(master_key)
    encryptor = Cipher(algorithms.AES(keys.encryption), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    envelope = {
        "v": 1,
        "job_id": job_id,
        "expires_at": expires_at,
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    if len(envelope["ciphertext"]) > MAX_ENVELOPE_CHARS:
        raise ValueError("Encrypted payload exceeds protocol limit")
    envelope["tag"] = base64.b64encode(
        hmac.new(keys.payload_mac, _canonical_envelope(envelope), hashlib.sha256).digest()
    ).decode("ascii")
    return envelope


def open_envelope(
    master_key: bytes,
    envelope: dict,
    *,
    expected_job_id: str | None = None,
    now: int | None = None,
) -> dict:
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
        raise EnvelopeError("Invalid encrypted envelope")
    job_id = envelope.get("job_id")
    expires_at = envelope.get("expires_at")
    if (
        envelope.get("v") != 1
        or not isinstance(job_id, str)
        or not _JOB_ID.fullmatch(job_id)
        or type(expires_at) is not int
        or (expected_job_id is not None and job_id != expected_job_id)
    ):
        raise EnvelopeError("Invalid encrypted envelope")

    iv = _decode_base64(envelope.get("iv"))
    ciphertext = _decode_base64(envelope.get("ciphertext"))
    supplied_tag = _decode_base64(envelope.get("tag"))
    if len(iv) != 16 or not ciphertext or len(ciphertext) % 16 or len(supplied_tag) != 32:
        raise EnvelopeError("Invalid encrypted envelope")
    keys = derive_keys(master_key)
    expected_tag = hmac.new(
        keys.payload_mac, _canonical_envelope(envelope), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_tag, expected_tag):
        raise EnvelopeError("Invalid encrypted envelope")

    current = int(time.time()) if now is None else int(now)
    if expires_at <= current:
        raise EnvelopeError("Encrypted envelope expired")
    try:
        decryptor = Cipher(algorithms.AES(keys.encryption), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        payload = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError("Invalid encrypted envelope") from exc
    if not isinstance(payload, dict):
        raise EnvelopeError("Invalid encrypted envelope")
    return payload

