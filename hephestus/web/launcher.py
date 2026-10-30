#!/usr/bin/env python3
"""Start the single-operator local web interface."""

import argparse
import webbrowser
from threading import Timer


def main():
    parser = argparse.ArgumentParser(description="Hephestus local operator interface")
    parser.add_argument(
        "--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1"
    )
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--config", help="YAML configuration path")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    from hephestus import config

    config.load(args.config)
    if config.CONFIG_ERRORS:
        parser.error("; ".join(config.CONFIG_ERRORS))
    # Every web launch begins locally; the authenticated UI owns remote setup.
    config.BACKEND = "local"
    from hephestus.web.app import ACCESS_TOKEN, app

    host = f"[{args.host}]" if args.host == "::1" else args.host
    url = f"http://{host}:{args.port}/#token={ACCESS_TOKEN}"
    print("Hephestus local operator URL (keep this private):", url)
    print("Run ended does not mean goal verified. Press Ctrl+C to stop the server.")
    if not args.no_browser:
        timer = Timer(1, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        app.run(
            host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True
        )
    except KeyboardInterrupt:
        pass
    finally:
        from hephestus.src import telemetry
        from hephestus.web.app import disconnect_worker
        from hephestus.web.runner import get_runner
        telemetry.sampler.close()
        disconnect_worker()
        try:
            remote = app.extensions.get("remote_session")
            if remote is not None:
                remote.close()
        finally:
            get_runner().shutdown()
            from hephestus.src import local_llm
            local_llm.unload_model()


if __name__ == "__main__":
    main()
