"""Supervised plan, propose, approve, execute and observe loop."""

import threading
import time
from collections import Counter

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from hephestus import config
from hephestus.src import (
    executor,
    human_gate,
    interpreter,
    observer,
    planner,
    policy_engine,
)
from hephestus.src.logger import Logger

console = Console()
COMPLETION_SIGNALS = {"TASK_COMPLETE", "GOAL_ACHIEVED", "COMPLETE", "DONE", "FINISHED"}


def _summarize_old_history(history_text):
    budget = max(256, config.MAX_CONTEXT_TOKENS)
    if len(history_text.encode("utf-8")) <= budget:
        return history_text
    tail = history_text.encode("utf-8")[-budget:]
    return "[Older history omitted; consult the audit log.]\n" + tail.decode(
        "utf-8", errors="ignore"
    )


def run(goal, context="", stop_event=None, run_id=None):
    """Run one supervised session; return a termination reason, never verified success."""
    if config.CONFIG_ERRORS:
        raise ValueError("Invalid configuration: " + "; ".join(config.CONFIG_ERRORS))
    if not isinstance(goal, str) or not 5 <= len(goal.strip()) <= 5000:
        raise ValueError("Goal must be 5..5000 characters")
    if not isinstance(context, str) or len(context) > 10000:
        raise ValueError("Context must be text of at most 10000 characters")
    stop_event = stop_event or threading.Event()
    start_time = time.monotonic()
    deadline = (
        start_time + config.RUN_TIMEOUT_SECONDS if config.RUN_TIMEOUT_SECONDS else None
    )
    executor.set_backend_by_name(config.BACKEND)
    executor.set_execution_limits(stop_event, deadline)
    log = Logger(run_id=run_id)
    log.log("start_run", {"goal": goal, "context_provided": bool(context)})
    log.log("config_snapshot", config.snapshot())
    console.print(Panel(Text(goal), title=f"Goal | backend: {config.BACKEND}"))
    if config.BACKEND == "local":
        console.print(
            "Local commands run on this host with your account permissions.",
            markup=False,
        )
    else:
        console.print(
            f"SSH target: {config.SSH_USERNAME}@{config.SSH_HOST}:{config.SSH_PORT}",
            markup=False,
        )
    history_text = ""
    policy_violations = 0
    failed_commands = Counter()
    failures = 0
    attempt = 0

    def stop_reason():
        if deadline is not None and time.monotonic() >= deadline:
            return "timeout"
        if stop_event.is_set() or policy_engine.check_kill_switch():
            return "stopped"
        return None

    def terminate(reason, **details):
        log.log(
            "run_terminated",
            dict(
                reason=reason,
                attempt=attempt,
                elapsed_seconds=time.monotonic() - start_time,
                verified=False,
                **details,
            ),
        )
        console.print(
            f"Run ended: {reason}. Review command output to assess the goal.",
            markup=False,
        )
        return reason

    try:
        for attempt in range(1, config.MAX_STEPS + 1):
            reason = stop_reason()
            if reason:
                return terminate(reason)
            original = len(history_text)
            history_text = _summarize_old_history(history_text)
            if len(history_text) < original:
                log.log(
                    "context_truncated",
                    {
                        "attempt": attempt,
                        "old_chars": original,
                        "new_chars": len(history_text),
                    },
                )
            log.log(
                "planner_request",
                {
                    "attempt": attempt,
                    "model": config.LLM_MODEL,
                    "max_tokens": config.MAX_TOKENS,
                    "temperature": config.TEMPERATURE,
                    "top_p": config.TOP_P,
                },
            )
            with console.status("Planning with local model..."):
                plan_text = planner.generate_plan(goal, history_text, context)
            reason = stop_reason()
            if reason:
                return terminate(reason)
            if plan_text is None:
                return terminate("planner_failure")
            log.log("planner_output", {"text": plan_text, "attempt": attempt})
            console.print(
                Panel(Markdown(plan_text), title=f"Plan {attempt}/{config.MAX_STEPS}")
            )
            commands = interpreter.extract_commands(plan_text)
            signals = [cmd for cmd in commands if cmd.upper() in COMPLETION_SIGNALS]
            commands = [
                cmd for cmd in commands if cmd.upper() not in COMPLETION_SIGNALS
            ]
            log.log("proposed_commands", {"commands": commands, "attempt": attempt})
            if signals and not commands:
                log.log(
                    "run_complete",
                    {
                        "status": "unverified",
                        "verified": False,
                        "reason": "model_completion_claim",
                        "attempts": attempt,
                        "elapsed_seconds": time.monotonic() - start_time,
                    },
                )
                console.print(
                    "Model claims completion. This is unverified; check the recorded output.",
                    markup=False,
                )
                return "model_completion_claim"
            if not commands:
                failures += 1
                log.log("no_commands", {"attempt": attempt})
                history_text += f"\nAttempt {attempt}: No executable proposals; use an explicit run: line or state a limitation.\n"
                if failures >= config.MAX_REPEATED_FAILURES:
                    return terminate("no_progress")
                continue
            failures = 0
            blocked = []
            for command in commands:
                allowed, reason = policy_engine.check_policy(command)
                if failed_commands[command] >= config.MAX_REPEATED_FAILURES:
                    allowed, reason = False, "Repeated unchanged failed command"
                log.log(
                    "policy_decision",
                    {
                        "command": command,
                        "allowed": allowed,
                        "reason": reason,
                        "attempt": attempt,
                    },
                )
                if not allowed:
                    blocked.append(f"{command}: {reason}")
            if blocked:
                policy_violations += 1
                history_text += (
                    f"\nAttempt {attempt}: Entire batch blocked: "
                    + "; ".join(blocked)
                    + "\n"
                )
                log.log(
                    "policy_block",
                    {
                        "attempt": attempt,
                        "count": policy_violations,
                        "reasons": blocked,
                    },
                )
                if policy_violations >= config.POLICY_MAX_VIOLATIONS:
                    return terminate("policy_halt")
                continue
            if stop_reason():
                return terminate(stop_reason())
            approved, rejected, aborted, goal_update = human_gate.approve_commands(
                commands
            )
            log.log(
                "approval_decision",
                {
                    "approved": approved,
                    "rejected": rejected,
                    "aborted": aborted,
                    "goal_update_provided": bool(goal_update),
                    "attempt": attempt,
                },
            )
            if stop_reason():
                return terminate(stop_reason())
            if aborted:
                return terminate("human_abort")
            if goal_update:
                context = (context + "\nOperator guidance: " + goal_update)[-10000:]
                history_text += (
                    f"\nAttempt {attempt}: Operator changed guidance. Replan.\n"
                )
                continue
            # A pluggable approver must not add or modify a proposed command.
            remaining = Counter(commands)
            for cmd in approved:
                if remaining[cmd] <= 0 or not policy_engine.check_policy(cmd)[0]:
                    return terminate("invalid_approval")
                remaining[cmd] -= 1
            observations = []
            for command in approved:
                if stop_reason():
                    return terminate(stop_reason())
                console.print(Panel(Text(command), title="Executing approved command"))
                result = executor.execute(command)
                log.log("execution_result", {**result, "attempt": attempt})
                for field in ("stdout", "stderr"):
                    if result.get(field):
                        console.print(Panel(Text(result[field][:4000]), title=field))
                summary = observer.summarise_result(
                    command,
                    result.get("stdout", ""),
                    result.get("stderr", ""),
                    result.get("exit_code", -1),
                )
                summary["executed"] = result.get("executed", False)
                log.log("observation", {**summary, "attempt": attempt})
                observations.append(summary)
                if result.get("exit_code") != 0:
                    failed_commands[command] += 1
                    # Stop a dependent batch after failure so later proposals can adapt.
                    break
                if stop_reason():
                    return terminate(stop_reason())
            history_text += (
                f"\nAttempt {attempt}: "
                + (
                    str(observations)
                    if observations
                    else "Operator rejected all commands."
                )
                + "\n"
            )
        return terminate("max_steps")
    except (KeyboardInterrupt, EOFError):
        stop_event.set()
        terminate("human_abort")
        raise
    except Exception as exc:
        log.log(
            "run_terminated",
            {
                "reason": "error",
                "error_type": type(exc).__name__,
                "verified": False,
                "attempt": attempt,
                "elapsed_seconds": time.monotonic() - start_time,
            },
        )
        raise
    finally:
        executor.close_backend()
        executor.set_execution_limits()
