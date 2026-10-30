#!/usr/bin/env python3
"""Run the disposable, memory-only Hephestus relay on local port 8000."""

from __future__ import annotations

import argparse

from hephestus.relay.server import BoundedRelayServer

from hephestus.relay.app import create_relay_app
from hephestus.relay.secrets import load_key_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-key", required=True, help="Path to relay-auth.key")
    parser.add_argument(
        "--host",
        choices=("127.0.0.1", "localhost", "::1"),
        default="127.0.0.1",
        help="Loopback interface exposed through Cloudflare Quick Tunnel",
    )
    parser.add_argument("--port", type=int, default=8000)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be 1..65535")
    app = create_relay_app(load_key_file(args.auth_key))
    print(f"Memory-only relay listening on http://{args.host}:{args.port}")
    print(f"Quick Tunnel: cloudflared tunnel --url http://{args.host}:{args.port}")
    print("No command or result payload is written to disk. Press Ctrl+C to stop.")
    BoundedRelayServer(args.host, args.port, app).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

