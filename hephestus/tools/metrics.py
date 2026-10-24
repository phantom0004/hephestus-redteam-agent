"""Print descriptive metrics and export CSV and JSON."""

import argparse
import csv
import json
from pathlib import Path

from hephestus.utils.metrics import generate_summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_dir", type=Path)
    args = parser.parse_args()
    if not args.log_dir.is_dir():
        parser.error("Log directory does not exist")
    summary = generate_summary(str(args.log_dir))
    if not summary["total_runs"]:
        print("No valid runs found")
        return
    print(f"Runs: {summary['total_runs']}; ended: {summary['completed_runs']}")
    print(
        f"Model completion claims: {summary['model_completion_claims']}; unverified runs: {summary['unverified_runs']}"
    )
    print(
        f"Commands proposed: {summary['total_proposed_commands']}; approved: {summary['total_approved_commands']}; executed: {summary['total_executed_commands']}"
    )
    print(
        f"Approval events: {summary['intervention_count']}; events/proposal: {summary['hir']:.3f}"
    )
    print(f"Mean duration: {summary['avg_duration']:.2f}s")
    print(
        "Success, isolation, autonomy and financial cost require independent evidence."
    )
    destination = args.log_dir / "metrics_summary.csv"
    fields = [
        "run_id",
        "model",
        "start_time",
        "end_time",
        "outcome",
        "verified",
        "model_completion_claim",
        "termination_reason",
        "dry_run",
        "duration",
        "attempts",
        "proposed_commands",
        "approved_commands",
        "executed_commands",
        "intervention_count",
        "hir",
    ]
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary["runs"])
    (args.log_dir / "metrics_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(f"Exported {destination}")


if __name__ == "__main__":
    main()
