"""
Backend validation tool for Hephestus.

Tests the backend connection before experiments.
Usage: python -m hephestus.tools.validate_backend --backend ssh
"""

import sys

from hephestus import config
from hephestus.src import executor


def validate_backend(backend_name: str) -> bool:
    """Test a backend by running a simple echo command."""
    print(f"Validating backend: {backend_name}")
    print("-" * 40)

    try:
        executor.set_backend_by_name(backend_name)
    except Exception as e:
        print(f"FAIL: Could not initialize backend: {e}")
        return False

    print("Backend initialized successfully")

    # Run test command
    test_cmd = "echo hephestus_backend_test"
    print(f"Running test command: {test_cmd}")

    result = executor.execute(test_cmd)

    if result.get("exit_code") == 0 and "hephestus_backend_test" in result.get(
        "stdout", ""
    ):
        print("PASS: Command executed successfully")
        print(f"  stdout: {result['stdout'].strip()}")
        return True
    else:
        print("FAIL: Unexpected result")
        print(f"  exit_code: {result.get('exit_code')}")
        print(f"  stdout: {result.get('stdout', '').strip()}")
        print(f"  stderr: {result.get('stderr', '').strip()}")
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hephestus Backend Validator")
    parser.add_argument(
        "--backend", default="local", help="Backend to validate (default: local)"
    )
    parser.add_argument("--config", help="Path to Hephestus YAML config file")
    args = parser.parse_args()

    if args.config:
        config.load(config_path=args.config)

    success = validate_backend(args.backend)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
