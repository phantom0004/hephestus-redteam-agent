from collections.abc import Callable
from dataclasses import dataclass

from hephestus import config
from hephestus.src.policy_engine import is_privesc_command

# Type alias for the approval function signature:
#   Input:  list of command strings
#   Output: (approved_cmds, rejected_cmds, aborted_flag, goal_update)
@dataclass(frozen=True)
class ApprovalDecision:
    """An operator decision bound to one immutable, possibly revised proposal."""
    proposals: tuple[str, ...]
    approved: tuple[str, ...]
    rejected: tuple[str, ...]
    aborted: bool
    goal_update: str | None

    def __iter__(self):
        # Retain the established four-value callback interface.
        yield list(self.approved)
        yield list(self.rejected)
        yield self.aborted
        yield self.goal_update


ApproverFunc = Callable[[list[str]], tuple[list[str], list[str], bool, str | None] | ApprovalDecision]

# Pluggable approver: when set (e.g. by the web UI or tests), this replaces
# the default interactive console prompt.  None = use console input.
_custom_approver: ApproverFunc | None = None


def set_approver(approver: ApproverFunc | None) -> None:
    """Set a custom approver function. Pass None to reset to console mode."""
    global _custom_approver
    _custom_approver = approver


def get_approver() -> ApproverFunc | None:
    """Get the current custom approver, if any."""
    return _custom_approver


def _console_approver(
    commands: list[str],
) -> tuple[list[str], list[str], bool, str | None]:
    """Default console-based human-in-the-loop approval gate.

    Three response modes:
      - Normal commands:    approved with "y"
      - Privesc commands:   require typing the full word "yes" (extra friction
                            to prevent accidental approval of sudo/su/runas)
      - Goal update:        type "u" to reject all commands and provide new
                            instructions for the AI to replan with
    """
    approved: list[str] = []
    rejected: list[str] = []
    require_supervisor = getattr(config, "REQUIRE_SUPERVISOR_APPROVAL", True)

    for idx, cmd in enumerate(commands, start=1):
        is_privesc = is_privesc_command(cmd)

        if is_privesc and require_supervisor:
            print("\n*** PRIVILEGE ESCALATION DETECTED ***")
            print("This command requires supervisor approval.")
            print(f"Command: {cmd}")
            answer = (
                input(
                    "Approve? Type 'yes' to approve, 'no' to reject, 'q' to abort, 'u' to update goal: "
                )
                .strip()
                .lower()
            )
            if answer in {"q", "quit", "abort", "exit"}:
                return approved, rejected, True, None
            if answer in {"u", "update"}:
                new_instructions = input("Enter updated instructions: ").strip()
                if new_instructions:
                    # Reject all remaining commands and return the goal update
                    remaining = [c for c in commands if c not in approved]
                    return [], remaining, False, new_instructions
                # Empty input, treat as rejection
                rejected.append(cmd)
                continue
            if answer == "yes":
                approved.append(cmd)
            else:
                rejected.append(cmd)
        else:
            print(f"[{idx}] {cmd}")
            answer = input("Approve? [y/N/q/u]: ").strip().lower()
            if answer in {"q", "quit", "abort", "exit"}:
                return approved, rejected, True, None
            if answer in {"u", "update"}:
                new_instructions = input("Enter updated instructions: ").strip()
                if new_instructions:
                    remaining = [c for c in commands if c not in approved]
                    return [], remaining, False, new_instructions
                rejected.append(cmd)
                continue
            if answer == "y":
                approved.append(cmd)
            else:
                rejected.append(cmd)
    return approved, rejected, False, None


def approve_commands(
    commands: list[str],
) -> tuple[list[str], list[str], bool, str | None]:
    """Approve commands using the current approver (custom or console).

    Returns:
        Tuple of (approved, rejected, aborted, goal_update).
        goal_update is a string with new user instructions, or None.
    """
    if _custom_approver is not None:
        return _custom_approver(commands)
    return _console_approver(commands)
