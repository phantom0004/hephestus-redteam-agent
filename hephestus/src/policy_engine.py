import os
import re

from hephestus import config

# File-based kill switch: dropping a file named HEPHESTUS_STOP in cwd or
# the log directory causes an immediate graceful halt of the agent loop.
KILL_SWITCH_FILENAME = "HEPHESTUS_STOP"
_kill_requested = False  # Programmatic kill switch (set via request_stop())

# OS-specific blocked primitives: these low-level commands could cause
# irreversible damage (disk wipe, format, reboot). The policy engine
# selects the appropriate list based on TARGET_OS config.
LINUX_BLOCKED_PRIMITIVES = ["dd", "mkfs", "reboot", "shutdown"]
WINDOWS_BLOCKED_PRIMITIVES = ["format", "diskpart", "bcdedit"]


def request_stop() -> None:
    global _kill_requested
    _kill_requested = True


def detect_target_os() -> str:
    """Detect target OS from config or backend type."""
    target = getattr(config, "TARGET_OS", "auto")
    if target and target != "auto":
        return target.lower()
    # Default to applying all rules
    return "auto"


def _get_blocked_primitives() -> list[str]:
    """Keep custom blocks active regardless of the target OS hint."""
    target = detect_target_os()
    defaults = (
        LINUX_BLOCKED_PRIMITIVES
        if target == "linux"
        else WINDOWS_BLOCKED_PRIMITIVES
        if target == "windows"
        else []
    )
    return list(dict.fromkeys(config.POLICY_BLOCKED_PRIMITIVES + defaults))


def is_privesc_command(command: str) -> bool:
    """Detect privilege escalation commands (sudo, su, runas, doas).

    When detected, the human_gate module requires the supervisor to type
    the full word "yes" instead of just "y" â€” an extra friction step that
    prevents accidental approval of high-risk operations.
    """
    privesc_patterns = [
        r"\bsudo\b",  # Linux: run as root
        r"\bsu\b\s",  # Linux: switch user (trailing \s avoids matching "su" in other words)
        r"\brunas\b",  # Windows: run as another user
        r"\bdoas\b",  # OpenBSD/Linux: simplified sudo alternative
    ]
    for pattern in privesc_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False


def check_policy(command: str) -> tuple[bool, str | None]:
    """Validate a command against the layered safety policy.

    Evaluation order (first match wins):
      1. Max command length  â€” rejects suspiciously long commands
      2. Allowlist (if set)  â€” only explicitly permitted patterns pass
      3. Blocked primitives  â€” OS-aware dangerous command names
      4. Denylist regexes    â€” catch dangerous patterns (rm -rf /, fork bombsâ€¦)

    Returns (allowed: bool, reason: str | None).
    """
    if len(command) > config.POLICY_MAX_COMMAND_LENGTH:
        return False, (
            f"Command exceeds max length ({len(command)} > {config.POLICY_MAX_COMMAND_LENGTH})"
        )

    if config.POLICY_ALLOWLIST:
        allow_match = False
        for pattern in config.POLICY_ALLOWLIST:
            try:
                if re.search(pattern, command, re.IGNORECASE):
                    allow_match = True
                    break
            except re.error:
                return False, f"Invalid allowlist regex: {pattern}"
        if not allow_match:
            return False, "Command does not match allowlist patterns"

    blocked_primitives = _get_blocked_primitives()
    for primitive in blocked_primitives:
        pattern = r"\b" + re.escape(primitive) + r"\b"
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Command uses blocked primitive: {primitive}"

    for pattern in config.POLICY_DENYLIST:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"Command matches denylist pattern: {pattern}"
        except re.error:
            return False, f"Invalid denylist regex: {pattern}"

    return True, None


def check_kill_switch() -> bool:
    """
    Checks for immediate stop signals.
    1. File-based: 'HEPHESTUS_STOP' in cwd or log_dir.
    """
    if _kill_requested:
        return True
    # Check CWD
    if os.path.exists(KILL_SWITCH_FILENAME):
        return True

    # Check Log Dir if it exists
    if config.LOG_DIR and os.path.exists(
        os.path.join(config.LOG_DIR, KILL_SWITCH_FILENAME)
    ):
        return True

    return False


def filter_commands(commands: list[str]) -> list[str]:
    """Filter a list of commands through the policy engine.

    SAFE DEFAULT: if *any* command in the batch is blocked, the entire
    batch is rejected (returns []).  This prevents partial execution
    that could leave the target in a corrupted or inconsistent state.
    """
    valid_commands = []
    for cmd in commands:
        allowed, reason = check_policy(cmd)
        if not allowed:
            return []  # Reject all if any is unsafe
        valid_commands.append(cmd)
    return valid_commands
