"""Model-driven safety and scope review of proposed commands.

Fixed deny-lists cannot anticipate every way a command can be harmful, so the
decision is delegated to the local model acting as a separate, cautious
reviewer with its own instructions. For each command that could change the
system it answers two questions about an authorized target
machine used for an authorized engagement:

  1. Could this change or destroy data, accounts, directory objects, security
     settings or recoverability in a way that is hard to undo?
  2. Does it stay inside the operator's scope / rules of engagement?

A command the reviewer is not confident is safe and in-scope is sent back to
the planner with the reason, so the model can choose a safer approach itself.
Two cheap static aids support, but never replace, the review: a read-only fast
path (so plain enumeration costs no model call) and a conservative
"looks like it changes something" check that forces a review and fails closed.
"""

import json
import re

from hephestus import config
from hephestus.src import local_llm

# Command verbs / tools whose normal use only reads or formats data. Anything
# not clearly in this set is treated as potentially state-changing and reviewed.
_READ_VERBS = {
    "get", "test", "select", "measure", "compare", "resolve", "find", "search", "show",
    "read", "format", "convertto", "convertfrom", "out", "sort", "group", "where", "foreach",
}
_PRINT_ONLY = {"write-output", "write-host", "write-verbose", "write-information", "write-warning"}
_READ_NATIVES = {
    "whoami", "hostname", "ipconfig", "systeminfo", "netstat", "nslookup", "ping", "tracert",
    "tasklist", "quser", "arp", "route", "getmac", "nltest", "echo", "dir", "type", "findstr",
    "ver", "klist", "gpresult", "driverquery", "ls", "cat", "pwd", "id", "uname", "ps", "env",
    "printenv", "head", "tail", "grep", "wc", "stat", "date", "whereis", "which",
}
_READ_ALIASES = {
    "gci", "gc", "gi", "gp", "gm", "gcm", "gsv", "gps", "gdr", "gal", "ft", "fl", "fw", "%", "?",
    "cd", "sl", "gl", "pushd", "popd", "cls", "clear", "select", "where", "sort", "measure", "group",
    "foreach", "ls", "dir", "type", "cat", "echo",
}
# A redirection or an obvious mutating token means "not read-only" regardless of the verb.
_MUTATION_HINT = re.compile(
    r"(?<![<>0-9])>{1,2}(?!&)|\b(remove|delete|del|erase|set|add|new|clear|disable|enable|stop|"
    r"start|restart|move|rename|copy|install|uninstall|register|unregister|format|reset|invoke|"
    r"import|export|out-file|takeown|icacls|cacls)\b",
    re.I,
)


def _first_token(segment: str) -> str:
    segment = segment.strip().lstrip("&|;([{ ").strip()
    match = re.match(r"[A-Za-z][\w\-\.]*", segment)
    return match.group(0).lower() if match else ""


def is_read_only(command: str) -> bool:
    """True only when every pipeline segment is a recognised read-only operation."""
    if not command or _MUTATION_HINT.search(command):
        return False
    # Split on statement/pipeline separators; every segment must be read-only.
    segments = [s for s in re.split(r"[|;\n]|&&|\|\|", command) if s.strip()]
    if not segments:
        return False
    for segment in segments:
        token = _first_token(segment)
        if not token:
            return False
        base = token.split("-")[0] if "-" in token else token
        read_ok = (
            token in _READ_NATIVES
            or token in _READ_ALIASES
            or token in _PRINT_ONLY
            or (base in _READ_VERBS and token not in {"set", "out-file"})
        )
        if not read_ok:
            return False
    return True


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "safe": {"type": "boolean"},
        "in_scope": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["safe", "in_scope", "reason"],
    "additionalProperties": False,
}

_REVIEWER_SYSTEM = (
    "You are an independent safety reviewer for an AUTHORIZED security engagement. Another model proposed a "
    "command to run on an authorized target computer that may hold sensitive files and user data. The operator has already "
    "authorized the engagement and stated the GOAL below. Your job is to let the authorized work proceed while "
    "protecting the machine from side effects that go beyond that goal, and to enforce the operator's scope.\n\n"
    "GOAL AWARENESS — THIS IS CENTRAL. The GOAL describes what the operator explicitly wants done. A command whose "
    "effect is the GOAL itself is EXPECTED, not a violation: set \"safe\": true and \"in_scope\": true for it, even "
    "when the technique is intrusive (injecting or overriding an environment variable, running a provided script, "
    "writing a config or property file, demonstrating command injection, popping a proof-of-concept message box, "
    "setting a benign marker or timestamp). Do NOT block a command merely because it carries out the stated goal, "
    "and do not demand a 'safer approach' when the operator asked for exactly this one.\n\n"
    "Judge the command's ACTUAL effect against the goal. Set \"safe\": false only when the command would cause "
    "serious, hard-to-undo harm BEYOND what the goal requires — for example it would delete, overwrite, encrypt or "
    "bulk-modify unrelated files or user data; disable, unlock or remove accounts, groups or group policy that the "
    "goal did not call for; reset passwords; weaken or disable security controls (antivirus, firewall, logging, "
    "backups, shadow copies, disk encryption); wipe or repartition a disk; or download and run untrusted external "
    "code. If the risky part IS the goal, it is in scope and permitted.\n\n"
    "Set \"in_scope\": false only if the command does something the operator's scope/ROE forbids or clearly reaches "
    "beyond both the goal and the scope. If no scope is given, judge in_scope against the goal and general "
    "authorization and set it true unless the command is plainly reckless and unrelated to the goal. When the "
    "command plausibly serves the goal and is not catastrophically destructive, allow it. Reply only as the JSON "
    "object requested, with a short, specific reason."
)


def _review_prompt(command: str, scope: str, goal: str = "") -> str:
    return (
        f"AUTHORIZED GOAL (what the operator explicitly wants done):\n{goal.strip() or '(none provided)'}\n\n"
        f"OPERATOR SCOPE / RULES OF ENGAGEMENT:\n{scope.strip() or '(none provided)'}\n\n"
        f"PROPOSED COMMAND (verbatim):\n{command}\n\n"
        "Return the JSON verdict. Remember: a command that performs the authorized goal is safe and in scope."
    )


def review_command(command: str, scope: str = "", goal: str = "", cancel_event=None) -> dict:
    """Ask the local model whether ``command`` is safe and in scope.

    The authorized ``goal`` is supplied so the reviewer treats commands that
    carry out that goal as expected rather than blocking them. Returns
    ``{"allowed": bool, "reason": str, "source": str}``. When the model cannot
    produce a verdict, the command is blocked (fail closed).
    """
    try:
        data = local_llm.create_chat_completion(
            messages=[
                {"role": "system", "content": _REVIEWER_SYSTEM},
                {"role": "user", "content": _review_prompt(command, scope, goal)},
            ],
            max_tokens=config.fit_max_tokens(320),
            temperature=0.0,
            top_p=0.9,
            response_format={"type": "json_object", "schema": REVIEW_SCHEMA},
            cancel_event=cancel_event,
        )
    except local_llm.ModelNotLoadedError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Safety reviewer error ({type(exc).__name__}): {exc}")
        return {"allowed": False, "reason": "The safety reviewer could not evaluate this command; blocked to fail safe.",
                "source": "reviewer_error"}
    choices = data.get("choices") or []
    content = (choices[0].get("message", {}).get("content") if choices else "") or ""
    try:
        verdict = json.loads(content)
    except ValueError:
        return {"allowed": False, "reason": "The safety reviewer returned an unreadable verdict; blocked to fail safe.",
                "source": "reviewer_error"}
    safe = bool(verdict.get("safe"))
    in_scope = bool(verdict.get("in_scope"))
    reason = str(verdict.get("reason") or "").strip()[:400]
    if safe and in_scope:
        return {"allowed": True, "reason": reason or "Reviewer: safe and in scope.", "source": "reviewer"}
    if not in_scope:
        return {"allowed": False,
                "reason": "Out of scope: " + (reason or "the command is outside the operator's rules of engagement."),
                "source": "scope"}
    return {"allowed": False,
            "reason": "Potentially destructive on a target computer: " + (reason or "hard-to-reverse change."),
            "source": "safety"}
