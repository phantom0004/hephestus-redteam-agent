"""
Batch experiment runner for Hephestus.

Runs multiple trials per goal, restoring VM snapshots between trials.

Usage: python -m hephestus.tools.batch_runner --goals goals.yaml --trials 3
"""

import csv
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import yaml

from hephestus import config
from hephestus.utils.metrics import parse_log_file
from hephestus.utils.vm_manager import VMManager


class BatchRunner:
    """Run batches of experiments with VM snapshot restore between trials."""

    def __init__(
        self,
        goal_file: str,
        num_trials: int = 3,
        output_dir: str = "batch_results",
        vm_name: str = None,
        snapshot_name: str = None,
    ):
        if type(num_trials) is not int or not 1 <= num_trials <= 1000:
            raise ValueError("num_trials must be 1..1000")
        if bool(vm_name) != bool(snapshot_name):
            raise ValueError("vm_name and snapshot_name must be supplied together")
        self.goal_file = goal_file
        self.num_trials = num_trials
        self.output_dir = output_dir
        self.vm_name = vm_name
        self.snapshot_name = snapshot_name
        self.vm_manager = VMManager() if vm_name else None

        os.makedirs(output_dir, exist_ok=True)

    def load_goals(self) -> list[dict[str, Any]]:
        """Load goals from a YAML file."""
        with open(self.goal_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        goals = []
        if isinstance(data, dict):
            for section_key, section_goals in data.items():
                if isinstance(section_goals, list):
                    for g in section_goals:
                        if not isinstance(g, dict) or not re.fullmatch(
                            r"[A-Za-z0-9_-]{1,64}", str(g.get("id", ""))
                        ):
                            raise ValueError(
                                "Each goal needs a safe unique id (letters, digits, underscore or hyphen)"
                            )
                        if (
                            not isinstance(g.get("goal"), str)
                            or not 5 <= len(g["goal"].strip()) <= 5000
                        ):
                            raise ValueError(
                                "Each goal needs 5..5000 characters of goal text"
                            )
                        g = {**g, "section": section_key}
                        goals.append(g)
        if not goals or len({g["id"] for g in goals}) != len(goals):
            raise ValueError("Goals must be nonempty with unique ids")
        return goals

    def _restore_snapshot(self) -> bool:
        """Restore VM to snapshot if configured."""
        if not self.vm_manager or not self.vm_name or not self.snapshot_name:
            return True

        result = self.vm_manager.restore_snapshot(self.vm_name, self.snapshot_name)
        if not result.get("success"):
            print(f"  Warning: Snapshot restore failed: {result.get('message')}")
            return False
        print(f"  Restored VM '{self.vm_name}' to snapshot '{self.snapshot_name}'")
        return True

    def _wait_for_vm_ready(self, timeout: int = 60) -> bool:
        """Wait for VM to be ready via SSH ping.

        After restoring a VirtualBox snapshot the guest OS needs a few
        seconds to boot and start its SSH daemon. This method polls with
        a simple "echo ready" over SSH until the VM responds or the
        timeout expires.
        """
        if not self.vm_name:
            return True

        if config.BACKEND != "ssh" or not config.SSH_HOST or not config.SSH_USERNAME:
            raise RuntimeError(
                "VM readiness requires the configured SSH execution host"
            )
        from hephestus.src.executor import SSHBackend

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            backend = SSHBackend()
            try:
                result = backend.execute("echo ready")
                if result["exit_code"] == 0 and "ready" in result["stdout"]:
                    return True
            finally:
                backend.close()
            time.sleep(2)
        return False

    def _run_single_trial(
        self, goal: dict[str, Any], trial_num: int
    ) -> dict[str, Any] | None:
        """Run a single trial for a goal.

        Each trial gets its own log directory so metrics can be parsed
        independently.  The global LOG_DIR is temporarily overridden and
        restored in a finally block to ensure cleanup even on exceptions.
        """
        from hephestus.src import agent

        goal_text = goal["goal"]
        goal_id = goal.get("id", "unknown")
        context = f"Goal ID: {goal_id}, Trial: {trial_num}"

        # Isolate each trial's logs for independent metrics parsing
        trial_log_dir = os.path.join(
            self.output_dir,
            "logs",
            f"{goal_id}_trial{trial_num}_{uuid.uuid4().hex[:8]}",
        )
        os.makedirs(trial_log_dir, exist_ok=True)

        # Temporarily redirect the global log directory
        old_log_dir = config.LOG_DIR
        config.LOG_DIR = trial_log_dir

        try:
            start_time = time.monotonic()
            agent.run(goal=goal_text, context=context)
            duration = time.monotonic() - start_time
        except KeyboardInterrupt:
            raise
        except Exception as e:
            duration = time.monotonic() - start_time
            print(f"  Trial {trial_num} error: {e}")
        finally:
            config.LOG_DIR = old_log_dir

        # Parse the log file for metrics
        import glob

        log_files = glob.glob(os.path.join(trial_log_dir, "*.jsonl"))
        if log_files:
            metrics = parse_log_file(log_files[0])
            metrics["trial"] = trial_num
            metrics["goal_id"] = goal_id
            metrics["measured_duration"] = duration
            return metrics
        return {
            "trial": trial_num,
            "goal_id": goal_id,
            "measured_duration": duration,
            "success": False,
        }

    def run(self) -> dict[str, Any]:
        """Run all goals for the configured number of trials."""
        goals = self.load_goals()
        print(f"Loaded {len(goals)} goals from {self.goal_file}")
        print(f"Running {self.num_trials} trials per goal")
        print("-" * 60)

        all_results: list[dict[str, Any]] = []
        goal_summaries: list[dict[str, Any]] = []

        for goal in goals:
            goal_id = goal.get("id", "unknown")
            goal_text = goal["goal"]
            print(f"\nGoal: {goal_id} - {goal_text}")

            trial_results = []
            for trial in range(1, self.num_trials + 1):
                print(f"  Trial {trial}/{self.num_trials}...")

                # Restore snapshot
                if not self._restore_snapshot():
                    raise RuntimeError(
                        "Snapshot restoration failed; no trial was executed"
                    )

                # Wait for VM
                if self.vm_name:
                    if not self._wait_for_vm_ready():
                        raise RuntimeError("VM readiness failed; no trial was executed")

                result = self._run_single_trial(goal, trial)
                if result:
                    trial_results.append(result)
                    all_results.append(result)

            # Aggregate per-goal metrics
            if trial_results:
                durations = [r.get("measured_duration", 0) for r in trial_results]
                attempts = [r.get("attempts", 0) for r in trial_results]

                # Calculate HIR
                total_proposed = sum(
                    r.get("proposed_commands", 0) for r in trial_results
                )
                total_interventions = sum(
                    r.get("intervention_count", 0) for r in trial_results
                )
                hir = total_interventions / total_proposed if total_proposed else 0.0

                summary = {
                    "goal_id": goal_id,
                    "goal": goal_text,
                    "trials": len(trial_results),
                    "model_completion_claims": sum(
                        r.get("model_completion_claim", False) for r in trial_results
                    ),
                    "success_rate": None,
                    "avg_duration": sum(durations) / len(durations),
                    "avg_attempts": sum(attempts) / len(attempts) if attempts else 0,
                    "hir": hir,
                }
                goal_summaries.append(summary)
                print(
                    f"  Results: {len(trial_results)} trials; outcomes require independent review; avg duration: {summary['avg_duration']:.1f}s"
                )

        # Export CSV
        self._export_csv(all_results, goal_summaries)
        return {"goals": goal_summaries, "total_trials": len(all_results)}

    def _export_csv(self, all_results: list[dict], goal_summaries: list[dict]) -> None:
        """Export results to CSV files."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # Per-trial CSV
        trial_csv = os.path.join(self.output_dir, f"batch_results_{ts}.csv")
        with open(trial_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "goal_id",
                    "trial",
                    "success",
                    "duration",
                    "attempts",
                    "proposed_commands",
                    "approved_commands",
                    "termination_reason",
                ]
            )
            for r in all_results:
                writer.writerow(
                    [
                        r.get("goal_id", ""),
                        r.get("trial", ""),
                        r.get("success", False),
                        r.get("measured_duration", 0),
                        r.get("attempts", 0),
                        r.get("proposed_commands", 0),
                        r.get("approved_commands", 0),
                        r.get("termination_reason", "unknown"),
                    ]
                )
        print(f"\nTrial results exported to: {trial_csv}")

        # Goal summary CSV
        summary_csv = os.path.join(self.output_dir, f"batch_summary_{ts}.csv")
        with open(summary_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "goal_id",
                    "goal",
                    "trials",
                    "success_rate",
                    "avg_duration",
                    "avg_attempts",
                    "hir",
                ]
            )
            for s in goal_summaries:
                writer.writerow(
                    [
                        s["goal_id"],
                        s["goal"],
                        s["trials"],
                        "unverified",
                        f"{s['avg_duration']:.1f}",
                        f"{s['avg_attempts']:.1f}",
                        f"{s['hir']:.2%}",
                    ]
                )
        print(f"Goal summary exported to: {summary_csv}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hephestus Batch Experiment Runner")
    parser.add_argument("--goals", required=True, help="Path to test goals YAML file")
    parser.add_argument(
        "--trials", type=int, default=3, help="Number of trials per goal (default: 3)"
    )
    parser.add_argument(
        "--output-dir", default="batch_results", help="Output directory for results"
    )
    parser.add_argument("--vm-name", help="VirtualBox VM name for snapshot management")
    parser.add_argument("--snapshot", help="Snapshot name to restore between trials")
    parser.add_argument("--config", help="Path to Hephestus YAML config file")
    args = parser.parse_args()

    config.load(config_path=args.config)
    if config.CONFIG_ERRORS:
        parser.error("; ".join(config.CONFIG_ERRORS))
    from hephestus.src.local_llm import validate_runtime

    validate_runtime()

    runner = BatchRunner(
        goal_file=args.goals,
        num_trials=args.trials,
        output_dir=args.output_dir,
        vm_name=args.vm_name,
        snapshot_name=args.snapshot,
    )

    try:
        results = runner.run()
        print(
            f"\nBatch complete: {results['total_trials']} total trials across {len(results['goals'])} goals"
        )
    except KeyboardInterrupt:
        print("\nBatch interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
