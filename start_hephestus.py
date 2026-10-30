#!/usr/bin/env python3
"""Launch Hephestus locally; Remote is enabled explicitly from the workspace."""
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _ensure_project_interpreter() -> None:
    """Re-exec with the bundled environment when called as `python3`/global Python."""
    interpreter = PROJECT_ROOT / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if not interpreter.is_file():
        return
    try:
        running = Path(sys.executable).resolve()
        bundled = interpreter.resolve()
    except OSError:
        return
    if running == bundled:
        return
    print(f"Switching to the project Python environment: {bundled}", flush=True)
    child = subprocess.Popen(
        [str(bundled), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=PROJECT_ROOT,
    )
    try:
        result = child.wait()
    except KeyboardInterrupt:
        # The console interrupt also reaches the child. Allow its cleanup first.
        try:
            result = child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5)
            result = 130
    raise SystemExit(result)


def main():
    _ensure_project_interpreter()
    os.chdir(PROJECT_ROOT)
    from web import main as launch_web
    launch_web()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
