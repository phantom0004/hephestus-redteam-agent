"""Descriptive metrics; planner completion is never independent success evidence."""

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path


def _parse_ts(value):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def parse_log_file(filepath):
    run_id = None
    start = end = None
    reason = "incomplete"
    attempts = proposed = approved = interventions = executions = 0
    verified = success = claimed = dry_run = False
    model = None
    with open(filepath, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or not isinstance(
                entry.get("data", {}), dict
            ):
                continue
            event, data = entry.get("event"), entry.get("data", {})
            if not isinstance(event, str) or not entry.get("run_id"):
                continue
            run_id = run_id or entry["run_id"]
            if entry["run_id"] != run_id:
                continue
            dry_run = dry_run or entry.get("dry_run") is True
            if event == "start_run":
                start = _parse_ts(entry.get("ts"))
            elif event == "run_complete":
                end = _parse_ts(entry.get("ts"))
                claimed = True
                reason = "model_completion_claim"
            elif event == "run_terminated":
                end = _parse_ts(entry.get("ts"))
                reason = str(data.get("reason", "terminated"))
            elif (
                event == "outcome_review"
                and data.get("verified") is True
                and data.get("evidence")
            ):
                verified = data.get("outcome") in {"met", "not_met"}
                success = data.get("outcome") == "met"
            elif event == "planner_output":
                attempt = data.get("attempt", 0)
                if type(attempt) is int:
                    attempts = max(attempts, attempt)
            elif event == "planner_request":
                model = data.get("model", model)
            elif event == "proposed_commands" and isinstance(
                data.get("commands"), list
            ):
                proposed += len(data["commands"])
            elif event == "approval_decision":
                interventions += 1
                if isinstance(data.get("approved"), list):
                    approved += len(data["approved"])
            elif event == "execution_result" and data.get("executed") is True:
                executions += 1
    if run_id is None:
        return {}
    duration = max(0.0, (end - start).total_seconds()) if start and end else 0.0
    if dry_run:
        verified = success = False
    return dict(
        run_id=run_id,
        model=model,
        success=success and verified,
        verified=verified,
        outcome=("met" if success else "not_met") if verified else "unverified",
        model_completion_claim=claimed,
        termination_reason=reason,
        dry_run=dry_run,
        duration=duration,
        attempts=attempts,
        proposed_commands=proposed,
        approved_commands=approved,
        executed_commands=executions,
        approval_rate=approved / proposed if proposed else 0.0,
        intervention_count=interventions,
        hir=interventions / proposed if proposed else 0.0,
        estimated_cost_eur=None,
        start_time=start.isoformat() if start else None,
        end_time=end.isoformat() if end else None,
        time_to_success=duration if success and verified else None,
    )


def _compute_stats(values):
    if not values:
        return dict(mean=0, stdev=0, min=0, max=0, median=0)
    return dict(
        mean=statistics.mean(values),
        stdev=statistics.stdev(values) if len(values) > 1 else 0,
        min=min(values),
        max=max(values),
        median=statistics.median(values),
    )


def generate_summary(log_dir):
    files = sorted(Path(log_dir).rglob("*.jsonl")) if Path(log_dir).is_dir() else []
    by_run = {}
    malformed_files = 0
    for path in files:
        try:
            record = parse_log_file(path)
        except (OSError, UnicodeError):
            malformed_files += 1
            continue
        if record:
            by_run.setdefault(record["run_id"], record)
    runs = list(by_run.values())
    count = len(runs)
    if not count:
        return dict(
            total_runs=0,
            files_found=len(files),
            skipped_files=malformed_files,
            message="No valid runs found",
        )
    completed = sum(r["termination_reason"] != "incomplete" for r in runs)
    reviewed = sum(r["verified"] for r in runs)
    successes = sum(r["success"] for r in runs)
    proposed = sum(r["proposed_commands"] for r in runs)
    approved = sum(r["approved_commands"] for r in runs)
    interventions = sum(r["intervention_count"] for r in runs)
    reasons = {}
    for run in runs:
        reasons[run["termination_reason"]] = (
            reasons.get(run["termination_reason"], 0) + 1
        )
    return dict(
        total_runs=count,
        completed_runs=completed,
        completion_rate=completed / count,
        reviewed_runs=reviewed,
        unverified_runs=count - reviewed,
        successes=successes,
        success_rate=successes / reviewed if reviewed else None,
        model_completion_claims=sum(r["model_completion_claim"] for r in runs),
        avg_attempts=statistics.mean(r["attempts"] for r in runs),
        avg_duration=statistics.mean(r["duration"] for r in runs),
        total_proposed_commands=proposed,
        total_approved_commands=approved,
        total_executed_commands=sum(r["executed_commands"] for r in runs),
        approval_rate=approved / proposed if proposed else 0.0,
        intervention_count=interventions,
        hir=interventions / proposed if proposed else 0.0,
        total_cost_eur=None,
        termination_reasons=reasons,
        duration_stats=_compute_stats([r["duration"] for r in runs]),
        skipped_files=malformed_files,
        runs=runs,
    )
