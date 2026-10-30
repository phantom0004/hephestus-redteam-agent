"""Validated YAML, environment and CLI configuration for local research runs."""

import json
import math
import os
import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Source checkouts keep user data beside the project. Installed packages use a
# writable per-user folder, never site-packages. An explicit override is useful
# for portable installations and isolated validation.
if os.getenv("HEPHESTUS_DATA_DIR"):
    DATA_ROOT = Path(os.environ["HEPHESTUS_DATA_DIR"]).expanduser().resolve()
elif (PROJECT_ROOT / "setup.py").is_file():
    DATA_ROOT = PROJECT_ROOT
elif os.name == "nt":
    DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Hephestus"
else:
    DATA_ROOT = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "hephestus"

DEFAULT_MODEL_FILENAME = "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
DEFAULT_MODEL_PATH = str(DATA_ROOT / "models" / DEFAULT_MODEL_FILENAME)
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
    "THINKING_LEVEL": ("runtime.thinking_level", "THINKING_LEVEL", "balanced", str),
    "LLM_CONTEXT_SIZE": ("llm.context_size", "LLM_CONTEXT_SIZE", 8192, int),
    "LLM_GPU_LAYERS": ("llm.gpu_layers", "LLM_GPU_LAYERS", 0, int),
    "LLM_GPU_AUTO": ("llm.gpu_auto", "LLM_GPU_AUTO", True, bool),
    "LLM_THREADS": (
        "llm.threads",
        "LLM_THREADS",
        max(1, (os.cpu_count() or 2) // 2),
        int,
    ),
    "LLM_BATCH_SIZE": ("llm.batch_size", "LLM_BATCH_SIZE", 512, int),
    "LLM_CHAT_FORMAT": ("llm.chat_format", "LLM_CHAT_FORMAT", "", str),
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
    "OBSERVATION_MAX_CHARS": (
        "runtime.observation_max_chars",
        "OBSERVATION_MAX_CHARS",
        2400,
        int,
    ),
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
    "SAFETY_REVIEW": (
        "runtime.safety_review",
        "SAFETY_REVIEW",
        False,
        bool,
    ),
    "COMMAND_POLICY": (
        "runtime.command_policy",
        "COMMAND_POLICY",
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
    "RELAY_URL": ("relay.url", "RELAY_URL", "http://127.0.0.1:8000", str),
    "RELAY_MASTER_KEY_PATH": (
        "relay.master_key_path",
        "RELAY_MASTER_KEY_PATH",
        str(DATA_ROOT / "secrets" / "worker.key"),
        str,
    ),
    "RELAY_REQUEST_TIMEOUT_SECONDS": (
        "relay.request_timeout_seconds",
        "RELAY_REQUEST_TIMEOUT_SECONDS",
        10,
        int,
    ),
    "RELAY_RESULT_POLL_INTERVAL": (
        "relay.result_poll_interval",
        "RELAY_RESULT_POLL_INTERVAL",
        0.5,
        float,
    ),
    "RELAY_JOB_TTL_SECONDS": (
        "relay.job_ttl_seconds",
        "RELAY_JOB_TTL_SECONDS",
        240,
        int,
    ),
}
LLM_MODEL: str = _SETTINGS["LLM_MODEL"][2]
MAX_TOKENS: int = _SETTINGS["MAX_TOKENS"][2]
TEMPERATURE: float = _SETTINGS["TEMPERATURE"][2]
TOP_P: float = _SETTINGS["TOP_P"][2]
THINKING_LEVEL: str = _SETTINGS["THINKING_LEVEL"][2]
LLM_CONTEXT_SIZE: int = _SETTINGS["LLM_CONTEXT_SIZE"][2]
LLM_GPU_LAYERS: int = _SETTINGS["LLM_GPU_LAYERS"][2]
LLM_GPU_AUTO: bool = _SETTINGS["LLM_GPU_AUTO"][2]
LLM_THREADS: int = _SETTINGS["LLM_THREADS"][2]
LLM_BATCH_SIZE: int = _SETTINGS["LLM_BATCH_SIZE"][2]
LLM_CHAT_FORMAT: str = _SETTINGS["LLM_CHAT_FORMAT"][2]
DRY_RUN: bool = _SETTINGS["DRY_RUN"][2]
MAX_STEPS: int = _SETTINGS["MAX_STEPS"][2]
RUN_TIMEOUT_SECONDS: int = _SETTINGS["RUN_TIMEOUT_SECONDS"][2]
BACKEND: str = _SETTINGS["BACKEND"][2]
COMMAND_TIMEOUT_SECONDS: int = _SETTINGS["COMMAND_TIMEOUT_SECONDS"][2]
MAX_CONTEXT_TOKENS: int = _SETTINGS["MAX_CONTEXT_TOKENS"][2]
OUTPUT_MAX_CHARS: int = _SETTINGS["OUTPUT_MAX_CHARS"][2]
OBSERVATION_MAX_CHARS: int = _SETTINGS["OBSERVATION_MAX_CHARS"][2]
MAX_REPEATED_FAILURES: int = _SETTINGS["MAX_REPEATED_FAILURES"][2]
TARGET_OS: str = _SETTINGS["TARGET_OS"][2]
REQUIRE_SUPERVISOR_APPROVAL: bool = _SETTINGS["REQUIRE_SUPERVISOR_APPROVAL"][2]
SAFETY_REVIEW: bool = _SETTINGS["SAFETY_REVIEW"][2]
COMMAND_POLICY: bool = _SETTINGS["COMMAND_POLICY"][2]
POLICY_MAX_COMMAND_LENGTH: int = _SETTINGS["POLICY_MAX_COMMAND_LENGTH"][2]
POLICY_MAX_VIOLATIONS: int = _SETTINGS["POLICY_MAX_VIOLATIONS"][2]
POLICY_ALLOWLIST: list = _SETTINGS["POLICY_ALLOWLIST"][2]
POLICY_DENYLIST: list = _SETTINGS["POLICY_DENYLIST"][2]
POLICY_BLOCKED_PRIMITIVES: list = _SETTINGS["POLICY_BLOCKED_PRIMITIVES"][2]
RELAY_URL: str = _SETTINGS["RELAY_URL"][2]
RELAY_MASTER_KEY_PATH: str = _SETTINGS["RELAY_MASTER_KEY_PATH"][2]
RELAY_REQUEST_TIMEOUT_SECONDS: int = _SETTINGS["RELAY_REQUEST_TIMEOUT_SECONDS"][2]
RELAY_RESULT_POLL_INTERVAL: float = _SETTINGS["RELAY_RESULT_POLL_INTERVAL"][2]
RELAY_JOB_TTL_SECONDS: int = _SETTINGS["RELAY_JOB_TTL_SECONDS"][2]

CONFIG_PATH = None
CONFIG_ERRORS = []

# Operator-tunable runtime settings are persisted here (plain configuration,
# never run evidence) so UI choices survive a server restart. Run event logs
# are stored separately in the ignored sessions directory, never in this file.
RUNTIME_SETTINGS_FILE = DATA_ROOT / "runtime-settings.json"

# Thinking profiles control how much room the model has to reason before it
# proposes a command. The planner lets the model reason first (see planner.py),
# so these token budgets govern real reasoning depth, not just the command
# string. Budgets are clamped to the active context window at apply time, so a
# profile is always valid even on a small-context model.
THINKING_PROFILES = {
    "quick": {"MAX_TOKENS": 512, "TEMPERATURE": 0.2, "TOP_P": 0.85},
    "balanced": {"MAX_TOKENS": 1024, "TEMPERATURE": 0.35, "TOP_P": 0.9},
    "deep": {"MAX_TOKENS": 2048, "TEMPERATURE": 0.5, "TOP_P": 0.95},
}

RUNTIME_CONTROL_ALIASES = {
    "max_tokens": "MAX_TOKENS",
    "temperature": "TEMPERATURE",
    "top_p": "TOP_P",
    "llm_context_size": "LLM_CONTEXT_SIZE",
    "llm_gpu_layers": "LLM_GPU_LAYERS",
    "llm_gpu_auto": "LLM_GPU_AUTO",
    "llm_threads": "LLM_THREADS",
    "llm_batch_size": "LLM_BATCH_SIZE",
    "max_steps": "MAX_STEPS",
    "command_timeout_seconds": "COMMAND_TIMEOUT_SECONDS",
    "run_timeout_seconds": "RUN_TIMEOUT_SECONDS",
    "max_repeated_failures": "MAX_REPEATED_FAILURES",
    "max_context_tokens": "MAX_CONTEXT_TOKENS",
    "output_max_chars": "OUTPUT_MAX_CHARS",
    "observation_max_chars": "OBSERVATION_MAX_CHARS",
    "safety_review": "SAFETY_REVIEW",
    "command_policy": "COMMAND_POLICY",
}

LOADER_SETTINGS = {
    "LLM_CONTEXT_SIZE",
    "LLM_GPU_LAYERS",
    "LLM_GPU_AUTO",
    "LLM_THREADS",
    "LLM_BATCH_SIZE",
}


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
            for section in ("llm", "runtime", "policy", "relay"):
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
    path_keys = {
        "LLM_MODEL",
        "RELAY_MASTER_KEY_PATH",
    }
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
        "OBSERVATION_MAX_CHARS",
        "MAX_REPEATED_FAILURES",
        "LLM_THREADS",
        "LLM_BATCH_SIZE",
        "POLICY_MAX_COMMAND_LENGTH",
        "POLICY_MAX_VIOLATIONS",
        "RELAY_REQUEST_TIMEOUT_SECONDS",
        "RELAY_JOB_TTL_SECONDS",
    ):
        if globals()[attr] < 1:
            CONFIG_ERRORS.append(f"{attr} must be positive")
    if RUN_TIMEOUT_SECONDS < 0 or LLM_GPU_LAYERS < -1:
        CONFIG_ERRORS.append("Invalid timeout or GPU layer count")
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
    if RELAY_RESULT_POLL_INTERVAL <= 0:
        CONFIG_ERRORS.append("relay.result_poll_interval must be positive")
    if BACKEND == 'relay' and RELAY_JOB_TTL_SECONDS <= COMMAND_TIMEOUT_SECONDS:
        CONFIG_ERRORS.append("relay.job_ttl_seconds must exceed the command timeout")
    if RELAY_JOB_TTL_SECONDS > 600:
        CONFIG_ERRORS.append("relay.job_ttl_seconds must not exceed 600")
    if BACKEND not in {"local", "relay"}:
        CONFIG_ERRORS.append("backend must be local or relay")
    if TARGET_OS not in {"auto", "linux", "windows"}:
        CONFIG_ERRORS.append("target_os must be auto, linux or windows")
    if THINKING_LEVEL not in THINKING_PROFILES:
        CONFIG_ERRORS.append("runtime.thinking_level must be quick, balanced or deep")
    for pattern in POLICY_ALLOWLIST + POLICY_DENYLIST:
        try:
            re.compile(pattern)
        except re.error:
            CONFIG_ERRORS.append("Invalid policy regular expression")
    # Saved operator UI settings win over YAML defaults, but explicit environment
    # or CLI values for the same control are always respected first.
    locked = {
        name
        for name, attr in RUNTIME_CONTROL_ALIASES.items()
        if _SETTINGS[attr][1] in os.environ or attr in cli
    }
    _apply_persisted_runtime_settings(locked)


def snapshot() -> dict:
    return {attr.lower(): globals()[attr] for attr in _SETTINGS} | {
        "config_path": CONFIG_PATH
    }


def runtime_settings() -> dict:
    """Return the small, non-persistent set of UI-tunable model controls."""
    return {
        **{name: globals()[attr] for name, attr in RUNTIME_CONTROL_ALIASES.items()},
        "thinking_level": THINKING_LEVEL,
        "thinking_profiles": list(THINKING_PROFILES),
    }


def _validate_runtime_controls(values: dict) -> None:
    if BACKEND == 'relay' and values['COMMAND_TIMEOUT_SECONDS'] >= RELAY_JOB_TTL_SECONDS:
        raise ValueError(f'Remote command timeout must be below the relay job lifetime ({RELAY_JOB_TTL_SECONDS}s)')
    if not 64 <= values["MAX_TOKENS"] <= 8192:
        raise ValueError("max_tokens must be between 64 and 8192")
    if not 0 <= values["TEMPERATURE"] <= 2:
        raise ValueError("temperature must be between 0 and 2")
    if not 0 < values["TOP_P"] <= 1:
        raise ValueError("top_p must be greater than 0 and at most 1")
    if not 512 <= values["LLM_CONTEXT_SIZE"] <= 32768:
        raise ValueError("llm_context_size must be between 512 and 32768")
    if values["MAX_TOKENS"] + 256 >= values["LLM_CONTEXT_SIZE"]:
        raise ValueError("llm_context_size must leave room for the prompt and output")
    if not -1 <= values["LLM_GPU_LAYERS"] <= 200:
        raise ValueError("llm_gpu_layers must be between -1 and 200")
    if not 1 <= values["LLM_THREADS"] <= 128:
        raise ValueError("llm_threads must be between 1 and 128")
    if not 32 <= values["LLM_BATCH_SIZE"] <= 2048:
        raise ValueError("llm_batch_size must be between 32 and 2048")
    if not 1 <= values["MAX_STEPS"] <= 50:
        raise ValueError("max_steps must be between 1 and 50")
    if not 5 <= values["COMMAND_TIMEOUT_SECONDS"] <= 3600:
        raise ValueError("command_timeout_seconds must be between 5 and 3600")
    if not 0 <= values["RUN_TIMEOUT_SECONDS"] <= 86400:
        raise ValueError("run_timeout_seconds must be between 0 and 86400 (0 = no limit)")
    if not 1 <= values["MAX_REPEATED_FAILURES"] <= 20:
        raise ValueError("max_repeated_failures must be between 1 and 20")
    if not 512 <= values["MAX_CONTEXT_TOKENS"] <= 32768:
        raise ValueError("max_context_tokens must be between 512 and 32768")
    if values["MAX_CONTEXT_TOKENS"] >= values["LLM_CONTEXT_SIZE"]:
        raise ValueError("max_context_tokens must be below llm_context_size")
    if not 1000 <= values["OUTPUT_MAX_CHARS"] <= 500000:
        raise ValueError("output_max_chars must be between 1000 and 500000")
    if not 200 <= values["OBSERVATION_MAX_CHARS"] <= 20000:
        raise ValueError("observation_max_chars must be between 200 and 20000")


def apply_runtime_overrides(overrides: dict) -> dict:
    """Apply validated UI controls for this process only; never rewrite YAML."""
    if not isinstance(overrides, dict) or not overrides:
        raise ValueError("Provide one or more runtime settings")
    candidate = {attr: globals()[attr] for attr in RUNTIME_CONTROL_ALIASES.values()}
    for name, value in overrides.items():
        attr = RUNTIME_CONTROL_ALIASES.get(name)
        if attr is None:
            raise ValueError(f"{name} is not a tunable runtime setting")
        candidate[attr] = _parse(value, _SETTINGS[attr][3])
    _validate_runtime_controls(candidate)
    for attr, value in candidate.items():
        globals()[attr] = value
    return runtime_settings()


def restore_runtime_settings(saved: dict) -> dict:
    """Restore a previously validated process-only runtime settings snapshot."""
    if not isinstance(saved, dict):
        raise ValueError("saved runtime settings must be a mapping")
    apply_runtime_overrides(
        {name: saved[name] for name in RUNTIME_CONTROL_ALIASES if name in saved}
    )
    level = saved.get("thinking_level", "balanced")
    if level not in THINKING_PROFILES:
        raise ValueError("saved thinking level is invalid")
    globals()["THINKING_LEVEL"] = level
    return runtime_settings()


def fit_max_tokens(desired: int) -> int:
    """Clamp a desired generation budget to leave room for the active context."""
    limit = max(64, min(8192, LLM_CONTEXT_SIZE - 512))
    try:
        desired = int(desired)
    except (TypeError, ValueError):
        desired = 64
    return max(64, min(desired, limit))


def set_thinking_level(level: str) -> dict:
    """Select a named inference profile for this process only.

    The profile's token budget is clamped to the active context window so that
    selecting any level is always valid, including on small-context models.
    """
    if not isinstance(level, str) or level not in THINKING_PROFILES:
        raise ValueError("thinking_level must be quick, balanced or deep")
    profile = THINKING_PROFILES[level]
    overrides = {name.lower(): value for name, value in profile.items()}
    overrides["max_tokens"] = fit_max_tokens(profile["MAX_TOKENS"])
    apply_runtime_overrides(overrides)
    globals()["THINKING_LEVEL"] = level
    return runtime_settings()


def default_runtime_settings() -> dict:
    """The balanced, out-of-the-box values for every operator-tunable control."""
    defaults = {name: _SETTINGS[attr][2] for name, attr in RUNTIME_CONTROL_ALIASES.items()}
    defaults["thinking_level"] = _SETTINGS["THINKING_LEVEL"][2]
    return defaults


def runtime_settings_path() -> Path:
    """Where UI settings persist; overridable so tests do not touch the project."""
    override = os.getenv("HEPHESTUS_RUNTIME_SETTINGS")
    return Path(override) if override else RUNTIME_SETTINGS_FILE


def persist_runtime_settings() -> bool:
    """Write the current operator-tunable controls to disk. Never writes evidence."""
    current = runtime_settings()
    payload = {name: current[name] for name in RUNTIME_CONTROL_ALIASES}
    payload["thinking_level"] = THINKING_LEVEL
    try:
        runtime_settings_path().parent.mkdir(parents=True, exist_ok=True)
        runtime_settings_path().write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return True
    except OSError:
        return False


def reset_runtime_settings() -> dict:
    """Restore the balanced defaults for every tunable control and persist them."""
    defaults = default_runtime_settings()
    apply_runtime_overrides({name: defaults[name] for name in RUNTIME_CONTROL_ALIASES})
    globals()["THINKING_LEVEL"] = defaults["thinking_level"]
    persist_runtime_settings()
    return runtime_settings()


def _apply_persisted_runtime_settings(locked_keys) -> None:
    """Layer any saved UI controls over resolved config, honouring env/CLI first."""
    path = runtime_settings_path()
    if not path.is_file():
        return
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict):
        return
    overrides = {
        name: saved[name]
        for name in RUNTIME_CONTROL_ALIASES
        if name in saved and name not in locked_keys
    }
    try:
        if overrides:
            apply_runtime_overrides(overrides)
        level = saved.get("thinking_level")
        if level in THINKING_PROFILES and "thinking_level" not in locked_keys:
            globals()["THINKING_LEVEL"] = level
    except (ValueError, TypeError, OverflowError):
        # A corrupt or out-of-range persisted file never blocks startup.
        pass


load()
