#!/usr/bin/env python3
"""Create an isolated environment and optionally install a verified Qwen GGUF."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_FILENAME = "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
MODEL_REPOSITORY = "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF"
MODEL_REVISION = "f86cb2c1fa58255f8052cc32aeede1b7482d4361"
MODEL_SHA256 = "cc324af070c2ecbfd324a30884d2f951a7ff756aba85cb811a6ec436933bb046"
MODEL_BYTES = 1117320768
MODEL_URL = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{MODEL_FILENAME}"
WHEEL_BACKENDS = ("cpu", "cu130", "cu132", "metal", "vulkan", "rocm72")


def _verified(path):
    if not path.is_file() or path.stat().st_size != MODEL_BYTES:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            return False
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == MODEL_SHA256


def download_model(destination=None):
    """Stream a pinned official model; publish it only after size/hash verification."""
    destination = Path(destination) if destination else ROOT / "models" / MODEL_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _verified(destination):
            print("Existing quick-start model verified; download skipped.")
            return destination
        raise RuntimeError(
            f"Existing model failed verification: {destination}. Move it aside before retrying."
        )
    if shutil.disk_usage(destination.parent).free < MODEL_BYTES + 128 * 1024 * 1024:
        raise RuntimeError(
            "At least 1.25 GB of free disk space is required for the model."
        )
    descriptor, temporary = tempfile.mkstemp(
        prefix=MODEL_FILENAME + ".", suffix=".part", dir=destination.parent
    )
    temporary = Path(temporary)
    try:
        request = urllib.request.Request(
            MODEL_URL, headers={"User-Agent": "Hephestus-Setup/0.2"}
        )
        received = 0
        digest = hashlib.sha256()
        with (
            os.fdopen(descriptor, "wb") as output,
            urllib.request.urlopen(request, timeout=60) as response,
        ):
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MODEL_BYTES:
                    raise RuntimeError("Download exceeds the expected model size")
                digest.update(chunk)
                output.write(chunk)
                print(
                    f"\rDownloading official Qwen GGUF: {received / MODEL_BYTES:.0%}",
                    end="",
                    flush=True,
                )
        print()
        if received != MODEL_BYTES or digest.hexdigest() != MODEL_SHA256:
            raise RuntimeError(
                "Model size or SHA256 does not match the pinned official file"
            )
        with temporary.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise RuntimeError("Downloaded file is not GGUF")
        temporary.replace(destination)
        print(f"Verified model installed: {destination}")
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def environment_python(directory):
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install_dependencies(directory, backend):
    directory = directory.expanduser().resolve()
    interpreter = environment_python(directory)
    if not interpreter.is_file():
        if directory.exists() and any(directory.iterdir()):
            raise RuntimeError(
                f"Refusing to create an environment inside a nonempty directory: {directory}"
            )
        print(f"Creating virtual environment: {directory}")
        venv.EnvBuilder(with_pip=True).create(directory)
    if (
        directory.resolve() == Path(sys.prefix).resolve()
        and sys.prefix == sys.base_prefix
    ):
        raise RuntimeError("Setup requires an isolated virtual environment")
    if not (directory / "pyvenv.cfg").is_file():
        raise RuntimeError(
            f"Selected interpreter is not inside a virtual environment: {directory}"
        )
    verification = subprocess.run(
        [
            str(interpreter),
            "-c",
            "import sys; print(int(sys.prefix != sys.base_prefix))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    if verification.stdout.strip() != "1":
        raise RuntimeError(
            "Selected interpreter is not an isolated virtual environment"
        )
    subprocess.run(
        [str(interpreter), "-m", "pip", "install", "--upgrade", "pip"], check=True
    )
    # Pin inference to the chosen upstream backend index; never silently build a GPU runtime.
    index = f"https://abetlen.github.io/llama-cpp-python/whl/{backend}"
    subprocess.run(
        [
            str(interpreter),
            "-m",
            "pip",
            "install",
            "--only-binary=llama-cpp-python",
            "--extra-index-url",
            index,
            "-r",
            str(ROOT / "requirements.txt"),
        ],
        check=True,
    )
    subprocess.run(
        [str(interpreter), "-m", "pip", "install", "--no-deps", "-e", str(ROOT)],
        check=True,
    )
    subprocess.run([str(interpreter), "-m", "pip", "check"], check=True)
    subprocess.run(
        [
            str(interpreter),
            "-c",
            "import click, rich, yaml, flask, paramiko; from llama_cpp import Llama; print('Runtime imports verified')",
        ],
        check=True,
    )
    return interpreter


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--venv",
        type=Path,
        default=ROOT / ".venv",
        help="Environment directory (default: .venv)",
    )
    parser.add_argument(
        "--llama-backend",
        choices=WHEEL_BACKENDS,
        default="cpu",
        help="Upstream wheel backend; GPU drivers are your responsibility",
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--download-model",
        action="store_true",
        help="Download the pinned quick-start Qwen without asking",
    )
    choice.add_argument(
        "--no-model", action="store_true", help="Install dependencies without a model"
    )
    parser.add_argument(
        "--model-only",
        action="store_true",
        help="Skip dependency installation; offer/download only the model",
    )
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):  # noqa: UP036 - bootstrap checks before package installation
        parser.error("Python 3.10 or newer is required; Python 3.12 is recommended")
    try:
        interpreter = environment_python(args.venv.resolve())
        if not args.model_only:
            interpreter = install_dependencies(args.venv, args.llama_backend)
        wants_model = args.download_model
        if not wants_model and not args.no_model:
            if not sys.stdin.isatty():
                print(
                    "Noninteractive input: model download skipped. Use --download-model to opt in."
                )
            else:
                print(
                    "Optional: Qwen2.5-Coder-1.5B-Instruct Q4_K_M, 1.12 GB, Apache-2.0."
                )
                print(f"Model card/license: https://huggingface.co/{MODEL_REPOSITORY}")
                wants_model = input(
                    "Download the quick-start model? [y/N]: "
                ).strip().lower() in {"y", "yes"}
        if wants_model:
            download_model()
        elif args.no_model or not wants_model:
            print(
                "Use your own instruct GGUF with --model PATH or LLM_MODEL; see README.md."
            )
        print("Setup finished.")
        if not args.model_only:
            print(
                f'CLI: "{interpreter}" -m hephestus.cli "List the current directory" --dry-run'
            )
            print(f'Web: "{interpreter}" "{ROOT / "web.py"}"')
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        urllib.error.URLError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        print(
            "See README.md for wheel/platform requirements and manual installation.",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "\nSetup interrupted; incomplete model data was removed.", file=sys.stderr
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
