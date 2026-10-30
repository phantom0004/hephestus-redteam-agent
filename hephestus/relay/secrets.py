"""Generate separate endpoint and relay-authentication key files."""

from __future__ import annotations

import argparse
import base64
import csv
import os
import secrets
import subprocess
from pathlib import Path

from hephestus.relay.crypto import derive_keys


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, base64.b64encode(data) + b"\n")
    finally:
        os.close(descriptor)
    try:
        _harden_acl(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _harden_acl(path: Path) -> None:
    if os.name != "nt":
        return
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    whoami = system_root / "System32" / "whoami.exe"
    icacls = system_root / "System32" / "icacls.exe"
    identity = subprocess.run(
        [str(whoami), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    rows = list(csv.reader([identity]))
    if len(rows) != 1 or len(rows[0]) != 2 or not rows[0][1].startswith("S-1-"):
        raise RuntimeError("Could not resolve the current Windows account SID")
    sid = rows[0][1]
    subprocess.run(
        [
            str(icacls),
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:F",
            "*S-1-5-18:F",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )


def write_secret_bundle(
    worker_key_path: str | Path,
    relay_auth_path: str | Path,
    *,
    master_key: bytes | None = None,
) -> None:
    master = master_key or secrets.token_bytes(32)
    keys = derive_keys(master)
    worker_path = Path(worker_key_path)
    relay_path = Path(relay_auth_path)
    if worker_path.exists() or relay_path.exists():
        raise FileExistsError("Refusing to overwrite an existing secret file")
    _write_new(worker_path, master)
    try:
        _write_new(relay_path, keys.relay_auth)
    except Exception:
        worker_path.unlink(missing_ok=True)
        raise


def load_key_file(path: str | Path) -> bytes:
    """Load one strict base64-encoded 32-byte key without accepting extra data."""
    raw = Path(path).read_text(encoding="ascii").strip()
    try:
        decoded = base64.b64decode(raw.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Key file must contain one base64-encoded key") from exc
    if len(decoded) != 32:
        raise ValueError("Key file must decode to exactly 32 bytes")
    return decoded


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create one worker key and one relay-only authentication key."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("secrets"),
        help="New key directory (default: secrets)",
    )
    args = parser.parse_args(argv)
    write_secret_bundle(
        args.output_dir / "worker.key", args.output_dir / "relay-auth.key"
    )
    print(f"Created {args.output_dir / 'worker.key'} (copy only this file to the remote PC)")
    print(f"Created {args.output_dir / 'relay-auth.key'} (keep on the main PC)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


