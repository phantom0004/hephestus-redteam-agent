import re
import os

from hephestus import config

# OS-specific blocked primitives: these low-level commands could cause
# irreversible damage (disk wipe, format, reboot). The policy engine
# selects the appropriate list based on TARGET_OS config.
LINUX_BLOCKED_PRIMITIVES = ["dd", "mkfs", "reboot", "shutdown"]
WINDOWS_BLOCKED_PRIMITIVES = ["format", "diskpart", "bcdedit"]


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
      Structural checks (ALWAYS run, not affected by COMMAND_POLICY):
        1. Max command length  - rejects suspiciously long commands
        2. Control characters   - rejects bidi/control-char smuggling
        3. Unfinished placeholder ("...") - rejects truncated proposals
        4. PowerShell syntax    - rejects commands that would not parse
      Dangerous-command policy (only when config.COMMAND_POLICY is enabled):
        5. Allowlist (if set)  - only explicitly permitted patterns pass
        6. Blocked primitives  - OS-aware dangerous command names
        7. Denylist regexes    - catch dangerous patterns (rm -rf /, fork bombs)

    The dangerous-command policy can be turned off globally (for minor
    engagements or tests); the structural checks and the operator's human
    approval gate remain in force regardless.

    Returns (allowed: bool, reason: str | None).
    """
    if len(command) > config.POLICY_MAX_COMMAND_LENGTH:
        return False, (
            f"Command exceeds max length ({len(command)} > {config.POLICY_MAX_COMMAND_LENGTH})"
        )

    if re.search(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F\u202A-\u202E\u2066-\u2069]", command):
        return False, "Command contains unsupported control characters"

    if "..." in command or "…" in command:
        return False, "Command contains an unfinished placeholder"

    if config.BACKEND == "relay" or os.name == "nt":
        from hephestus.src.command_validation import powershell_syntax_error
        error = powershell_syntax_error(command)
        if error:
            return False, error

    # Everything above is structural validity (length, control characters,
    # unfinished placeholders, PowerShell syntax) and always runs so a command
    # stays well-formed. Everything below is the dangerous-command policy
    # (allowlist / blocked primitives / denylist), which the operator can
    # disable globally for minor engagements or tests. When it is off, the human
    # approval gate remains the backstop for dangerous commands.
    if not config.COMMAND_POLICY:
        return True, None

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
        # Match the primitive only as a standalone command token: not preceded by
        # '-', '.' or a word char (so '-Format', '.Format', 'Reformat' are safe)
        # and not followed by '-' or a word char (so 'Format-Table', 'formatting'
        # are safe). The real destructive 'format C:' / 'diskpart' still matches.
        pattern = r"(?<![-.\w])" + re.escape(primitive) + r"(?![-\w])"
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
    """Legacy compatibility hook; active runs use their in-memory stop event."""
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
