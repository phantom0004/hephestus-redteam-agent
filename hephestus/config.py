"""Validated YAML, environment and CLI configuration for local research runs."""

import math
import os
import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_FILENAME = "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
DEFAULT_MODEL_PATH = str(PROJECT_ROOT / "models" / DEFAULT_MODEL_FILENAME)
DEFAULT_LOG_DIR = str(PROJECT_ROOT / "logs")
DEFAULT_POLICY_DENYLIST = [
    # Linux/Unix dangerous patterns
    r"rm\s+-[rRf]+\s+/",  # No root nuking
    r">\s*/dev/sda",  # No overwriting devices
    r"mkfs",  # No formatting
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\};:",  # Fork bomb
    r"nc\s+-e",  # Netcat with execution
    r"bash\s+-i",  # Interactive bash reverse shells
    r"shutdown",  # No shutting down the host
    r"reboot",  # No rebooting
    # Windows dangerous patterns
    r"format\s+[a-zA-Z]:",  # Format drive
    r"del\s+/[sS]",  # Recursive delete
    r"rd\s+/[sS]",  # Recursive directory removal
    r"rmdir\s+/[sS]",  # Recursive directory removal
    r"reg\s+delete",  # Registry deletion
    r"bcdedit",  # Boot configuration edit
    r"cipher\s+/[wW]",  # Secure wipe
]
DEFAULT_POLICY_BLOCKED_PRIMITIVES = [
    # Linux primitives
    "dd",
    "mkfs",
    "reboot",
    "shutdown",
    # Windows primitives
    "format",
    "diskpart",
    "bcdedit",
]


# Attribute: (YAML section/key, environment variable, default, value type)
_SETTINGS = {
    "LLM_MODEL": ("llm.model", "LLM_MODEL", DEFAULT_MODEL_PATH, str),
    "MAX_TOKENS": ("llm.max_tokens", "LLM_MAX_TOKENS", 640, int),
    "TEMPERATURE": ("llm.temperature", "LLM_TEMPERATURE", 0.45, float),
    "TOP_P": ("llm.top_p", "LLM_TOP_P", 0.9, float),
    "LLM_CONTEXT_SIZE": ("llm.context_size", "LLM_CONTEXT_SIZE", 8192, int),
    "LLM_GPU_LAYERS": ("llm.gpu_layers", "LLM_GPU_LAYERS", 0, int),
    "LLM_THREADS": (
        "llm.threads",
        "LLM_THREADS",
        max(1, (os.cpu_count() or 2) // 2),
        int,
    ),
    "LLM_BATCH_SIZE": ("llm.batch_size", "LLM_BATCH_SIZE", 512, int),
    "LLM_CHAT_FORMAT": ("llm.chat_format", "LLM_CHAT_FORMAT", "", str),
    "LOG_DIR": ("runtime.log_dir", "LOG_DIR", DEFAULT_LOG_DIR, str),
    "DRY_RUN": ("runtime.dry_run", "DRY_RUN", False, bool),
    "MAX_STEPS": ("runtime.max_steps", "MAX_STEPS", 15, int),
    "RUN_TIMEOUT_SECONDS": ("runtime.timeout_seconds", "RUN_TIMEOUT_SECONDS", 0, int),
    "BACKEND": ("runtime.backend", "BACKEND", "local", str),
    "COMMAND_TIMEOUT_SECONDS": (
        "runtime.command_timeout_seconds",
        "COMMAND_TIMEOUT_SECONDS",
        120,
        int,
    ),
    "MAX_CONTEXT_TOKENS": (
        "runtime.max_context_tokens",
        "MAX_CONTEXT_TOKENS",
        5000,
        int,
    ),
    "OUTPUT_MAX_CHARS": ("runtime.output_max_chars", "OUTPUT_MAX_CHARS", 32000, int),
    "MAX_REPEATED_FAILURES": (
        "runtime.max_repeated_failures",
        "MAX_REPEATED_FAILURES",
        3,
        int,
    ),
    "TARGET_OS": ("runtime.target_os", "TARGET_OS", "auto", str),
    "REQUIRE_SUPERVISOR_APPROVAL": (
        "runtime.require_supervisor_approval",
        "REQUIRE_SUPERVISOR_APPROVAL",
        True,
        bool,
    ),
    "POLICY_MAX_COMMAND_LENGTH": (
        "policy.max_command_length",
        "POLICY_MAX_COMMAND_LENGTH",
        4000,
        int,
    ),
    "POLICY_MAX_VIOLATIONS": ("policy.max_violations", "POLICY_MAX_VIOLATIONS", 3, int),
    "POLICY_ALLOWLIST": ("policy.allowlist", "POLICY_ALLOWLIST", [], list),
    "POLICY_DENYLIST": (
        "policy.denylist",
        "POLICY_DENYLIST",
        DEFAULT_POLICY_DENYLIST,
        list,
    ),
    "POLICY_BLOCKED_PRIMITIVES": (
        "policy.blocked_primitives",
        "POLICY_BLOCKED_PRIMITIVES",
        DEFAULT_POLICY_BLOCKED_PRIMITIVES,
        list,
    ),
    "SSH_HOST": ("ssh.host", "SSH_HOST", "", str),
    "SSH_PORT": ("ssh.port", "SSH_PORT", 22, int),
    "SSH_USERNAME": ("ssh.username", "SSH_USERNAME", "", str),
    "SSH_KEY_PATH": ("ssh.key_path", "SSH_KEY_PATH", "", str),
    "SSH_KNOWN_HOSTS": ("ssh.known_hosts", "SSH_KNOWN_HOSTS", "", str),
    "SSH_TIMEOUT_SECONDS": ("ssh.timeout_seconds", "SSH_TIMEOUT_SECONDS", 30, int),
}
LLM_MODEL: str = _SETTINGS["LLM_MODEL"][2]
MAX_TOKENS: int = _SETTINGS["MAX_TOKENS"][2]
TEMPERATURE: float = _SETTINGS["TEMPERATURE"][2]
TOP_P: float = _SETTINGS["TOP_P"][2]
LLM_CONTEXT_SIZE: int = _SETTINGS["LLM_CONTEXT_SIZE"][2]
LLM_GPU_LAYERS: int = _SETTINGS["LLM_GPU_LAYERS"][2]
LLM_THREADS: int = _SETTINGS["LLM_THREADS"][2]
LLM_BATCH_SIZE: int = _SETTINGS["LLM_BATCH_SIZE"][2]
LLM_CHAT_FORMAT: str = _SETTINGS["LLM_CHAT_FORMAT"][2]
LOG_DIR: str = _SETTINGS["LOG_DIR"][2]
DRY_RUN: bool = _SETTINGS["DRY_RUN"][2]
MAX_STEPS: int = _SETTINGS["MAX_STEPS"][2]
RUN_TIMEOUT_SECONDS: int = _SETTINGS["RUN_TIMEOUT_SECONDS"][2]
BACKEND: str = _SETTINGS["BACKEND"][2]
COMMAND_TIMEOUT_SECONDS: int = _SETTINGS["COMMAND_TIMEOUT_SECONDS"][2]
MAX_CONTEXT_TOKENS: int = _SETTINGS["MAX_CONTEXT_TOKENS"][2]
OUTPUT_MAX_CHARS: int = _SETTINGS["OUTPUT_MAX_CHARS"][2]
MAX_REPEATED_FAILURES: int = _SETTINGS["MAX_REPEATED_FAILURES"][2]
TARGET_OS: str = _SETTINGS["TARGET_OS"][2]
REQUIRE_SUPERVISOR_APPROVAL: bool = _SETTINGS["REQUIRE_SUPERVISOR_APPROVAL"][2]
POLICY_MAX_COMMAND_LENGTH: int = _SETTINGS["POLICY_MAX_COMMAND_LENGTH"][2]
POLICY_MAX_VIOLATIONS: int = _SETTINGS["POLICY_MAX_VIOLATIONS"][2]
POLICY_ALLOWLIST: list = _SETTINGS["POLICY_ALLOWLIST"][2]
POLICY_DENYLIST: list = _SETTINGS["POLICY_DENYLIST"][2]
POLICY_BLOCKED_PRIMITIVES: list = _SETTINGS["POLICY_BLOCKED_PRIMITIVES"][2]
SSH_HOST: str = _SETTINGS["SSH_HOST"][2]
SSH_PORT: int = _SETTINGS["SSH_PORT"][2]
SSH_USERNAME: str = _SETTINGS["SSH_USERNAME"][2]
SSH_KEY_PATH: str = _SETTINGS["SSH_KEY_PATH"][2]
SSH_KNOWN_HOSTS: str = _SETTINGS["SSH_KNOWN_HOSTS"][2]
SSH_TIMEOUT_SECONDS: int = _SETTINGS["SSH_TIMEOUT_SECONDS"][2]

CONFIG_PATH = None
CONFIG_ERRORS = []


def _parse(value: Any, kind: type) -> Any:
    if kind is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y"}:
                return True
            if text in {"0", "false", "no", "n"}:
                return False
        raise ValueError("expected true or false")
    if kind is int:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("expected an integer")
        return int(value)
    if kind is float:
        if isinstance(value, bool):
            raise ValueError("expected a number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("expected a finite number")
        return number
    if kind is list:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError("expected a list of strings")
        return value.copy()
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value


def load(config_path=None, overrides=None) -> None:
    """Resolve defaults < YAML < environment < explicit CLI overrides.

    Relative model/log/key paths in YAML resolve beside that YAML file.
    Environment and CLI paths resolve from the current working directory.
    """
    global CONFIG_PATH, CONFIG_ERRORS
    CONFIG_ERRORS = []
    CONFIG_PATH = config_path or os.getenv("HEPHESTUS_CONFIG")
    data = {}
    if CONFIG_PATH:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            if not isinstance(data, dict):
                raise ValueError("configuration must be a mapping")
            for section in ("llm", "runtime", "policy", "ssh"):
                if section in data and not isinstance(data[section], dict):
                    raise ValueError(f"{section} must be a mapping")
        except (OSError, ValueError, yaml.YAMLError) as exc:
            CONFIG_ERRORS.append(f"Cannot load configuration: {exc}")
            data = {}
    cli_names = {"llm_model": "LLM_MODEL", "timeout_seconds": "RUN_TIMEOUT_SECONDS"}
    cli = {
        cli_names.get(k, k.upper()): v
        for k, v in (overrides or {}).items()
        if v is not None
    }
    path_keys = {"LLM_MODEL", "LOG_DIR", "SSH_KEY_PATH", "SSH_KNOWN_HOSTS"}
    for attr, (yaml_key, env_key, default, kind) in _SETTINGS.items():
        section, key = yaml_key.split(".")
        value = data.get(section, {}).get(key, data.get(attr.lower(), default))
        from_yaml = value is not default
        if env_key in os.environ:
            value, from_yaml = os.environ[env_key], False
        if attr in cli:
            value, from_yaml = cli[attr], False
        try:
            parsed = _parse(value, kind)
            if attr in path_keys and parsed:
                path = Path(parsed).expanduser()
                if from_yaml and not path.is_absolute():
                    path = Path(CONFIG_PATH).resolve().parent / path
                parsed = str(path.resolve())
            globals()[attr] = parsed
        except (ValueError, TypeError, OverflowError) as exc:
            CONFIG_ERRORS.append(f"Invalid {yaml_key} / {env_key}: {exc}")
            globals()[attr] = default.copy() if isinstance(default, list) else default
    for attr in (
        "MAX_TOKENS",
        "MAX_STEPS",
        "COMMAND_TIMEOUT_SECONDS",
        "MAX_CONTEXT_TOKENS",
        "OUTPUT_MAX_CHARS",
        "MAX_REPEATED_FAILURES",
        "LLM_THREADS",
        "LLM_BATCH_SIZE",
        "POLICY_MAX_COMMAND_LENGTH",
        "POLICY_MAX_VIOLATIONS",
        "SSH_TIMEOUT_SECONDS",
    ):
        if globals()[attr] < 1:
            CONFIG_ERRORS.append(f"{attr} must be positive")
    if RUN_TIMEOUT_SECONDS < 0 or not 1 <= SSH_PORT <= 65535 or LLM_GPU_LAYERS < -1:
        CONFIG_ERRORS.append("Invalid timeout, SSH port or GPU layer count")
    if LLM_CONTEXT_SIZE < 512 or MAX_TOKENS + 256 >= LLM_CONTEXT_SIZE:
        CONFIG_ERRORS.append(
            "llm.context_size must leave room for the prompt and generated tokens"
        )
    if MAX_CONTEXT_TOKENS >= LLM_CONTEXT_SIZE:
        CONFIG_ERRORS.append(
            "runtime.max_context_tokens must be below llm.context_size"
        )
    if not 0 <= TEMPERATURE <= 2 or not 0 < TOP_P <= 1:
        CONFIG_ERRORS.append("temperature must be 0..2 and top_p must be >0..1")
    if BACKEND not in {"local", "ssh"}:
        CONFIG_ERRORS.append("backend must be local or ssh")
    if TARGET_OS not in {"auto", "linux", "windows"}:
        CONFIG_ERRORS.append("target_os must be auto, linux or windows")
    if BACKEND == "ssh" and (not SSH_HOST or not SSH_USERNAME):
        CONFIG_ERRORS.append("ssh.host and ssh.username are required for SSH")
    for pattern in POLICY_ALLOWLIST + POLICY_DENYLIST:
        try:
            re.compile(pattern)
        except re.error:
            CONFIG_ERRORS.append("Invalid policy regular expression")


def snapshot() -> dict:
    return {attr.lower(): globals()[attr] for attr in _SETTINGS} | {
        "config_path": CONFIG_PATH
    }


load()
