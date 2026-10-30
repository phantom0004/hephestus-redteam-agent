"""One supervised worker and one-use approval proposals per process."""

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from collections import deque
from enum import Enum

from hephestus import config
from hephestus.src import agent, human_gate, policy_engine, local_llm
from hephestus.src.logger import add_log_callback, remove_log_callback
from hephestus.web.session_store import SessionStore


class AgentStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"


# Why a run ended, in operator language. Operator decisions are kept distinct:
# rejecting a command is not the same as quitting the remote worker, aborting
# from the approval dialog, or pressing Stop.
TERMINATION_REASONS = {
    "operator_completed": ("Marked complete by operator", "The operator ended the task with Finish."),
    "stopped": ("Stopped by operator", "The operator pressed Stop & disconnect in the workspace."),
    "human_abort": ("Aborted by operator", "The operator chose Abort task in the approval dialog."),
    "quick_task_rejected": ("Command rejected by operator",
                            "The operator rejected the quick-task command in the approval dialog."),
    "operator_rejected": ("Command rejected on remote PC",
                          "The remote-PC operator answered N (reject); the command was not executed."),
    "operator_quit": ("Remote worker quit by operator",
                      "The remote-PC operator answered Q (quit); the worker shut down and the command was not executed."),
    "quick_task_complete": ("Quick task succeeded", "The single quick-task command ran with exit code 0."),
    "model_completion_claim": ("Model reports completion",
                               "The model claimed the goal is met; this is unverified until the operator reviews the evidence."),
    "max_steps": ("Iteration limit reached", "The run used every allowed iteration without finishing."),
    "timeout": ("Run timed out", "The run exceeded its configured time limit."),
    "planner_failure": ("Model failed to plan", "The local model could not produce a usable proposal."),
    "no_progress": ("No progress", "The model repeatedly failed to propose a usable command."),
    "duplicate_no_progress": ("Stuck repeating commands", "The model kept proposing commands that were already tried."),
    "invalid_approval": ("Invalid approval", "The approval did not match the proposal and was refused."),
    "execution_unknown": ("Remote execution outcome unknown", "The command may have run but no verified result was received. Automatic retries stopped; inspect the target before continuing."),
    "error": ("Run error", "The run stopped because of an internal error."),
    "server_interrupted": ("Interrupted by server shutdown",
                           "The server stopped while this run was active; the evidence up to that point is preserved."),
}
FAILED_REASONS = {"planner_failure", "no_progress", "duplicate_no_progress", "max_steps", "timeout",
                  "invalid_approval", "server_interrupted", "error", "execution_unknown"}
ACTIVE_STATUSES = {"running", "waiting_for_user", "stopping"}


def termination_label(reason):
    if not reason:
        return None, None
    return TERMINATION_REASONS.get(reason, (str(reason).replace("_", " ").capitalize(), ""))


def sessions_dir():
    override = os.getenv("HEPHESTUS_SESSIONS_DIR")
    return override or str(config.DATA_ROOT / "sessions")


_STOPWORDS = set(
    "the and for are was were what why how did does this that with from have has had into about which "
    "when then than them they their there here your you can could would should will shall its it's "
    "session command commands output result results run happen happened mean means explain tell show "
    "please give list any all not but out get got use used".split()
)
_RECENCY_WORDS = {"last", "latest", "recent", "final", "now", "current", "end", "ended", "finally"}


class AgentRunner:
    def __init__(self, max_logs=5000, storage_dir=None):
        self.status = AgentStatus.IDLE
        self.logs = deque(maxlen=max_logs)
        self.pending_commands = []
        self.user_response = None
        self.error_message = None
        self.proposal_id = None
        self.privileged_indices = []
        self.run_id = None
        self.termination_reason = None
        # Export and current-session lookup compose lock-protected helpers.
        # Reentrancy keeps those read paths from deadlocking the UI thread.
        self._lock = threading.RLock()
        self._approval_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread = None
        self._sequence = 0
        self._deadline = None
        self.current_step = 0
        self.phase = "idle"
        self.last_activity_at = None
        self.mode = "goal"
        self.goal = ""
        self.context = ""
        self.title = ""
        self.created_at = None
        self.sessions = []
        self._completion_event = threading.Event()
        self._store = SessionStore(storage_dir) if storage_dir else None
        self._dirty = False
        self._save_lock = threading.Lock()
        self._deleted_ids = set()
        if self._store is not None:
            self._load_saved_sessions()
            threading.Thread(target=self._flush_loop, daemon=True, name="hephestus-session-saver").start()

    # ---------- persistence ----------
    def _load_saved_sessions(self):
        for record in self._store.load_all():
            if record.get("status") in ACTIVE_STATUSES:
                record["status"] = "error"
                record["termination_reason"] = "server_interrupted"
                self._store.save({k: v for k, v in record.items() if k != "schema"})
            logs = record.pop("logs", [])
            record.pop("schema", None)
            record["event_count"] = len(logs)
            record["persisted"] = True
            self.sessions.append(record)

    def _current_record(self):
        return {
            "run_id": self.run_id, "goal": self.goal, "context": self.context,
            "title": self.title, "mode": self.mode, "status": self.status.value,
            "termination_reason": self.termination_reason, "created_at": self.created_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "logs": [entry.copy() for entry in self.logs],
        }

    def _write(self, record):
        if self._store is None or not record.get("run_id"):
            return
        with self._save_lock:
            if record["run_id"] in self._deleted_ids:
                return
            try:
                self._store.save(record)
            except OSError as exc:
                print(f"Hephestus could not save session {record['run_id']}: {exc}", file=sys.stderr, flush=True)

    def flush(self):
        """Write the current session now if it changed since the last save."""
        with self._lock:
            if not self._dirty or not self.run_id:
                return
            self._dirty = False
            record = self._current_record()
        self._write(record)

    def _flush_loop(self):
        while True:
            time.sleep(1.0)
            try:
                self.flush()
            except Exception as exc:  # noqa: BLE001 - the saver must never die
                print(f"Hephestus session saver error: {exc}", file=sys.stderr, flush=True)

    def _mark_dirty(self):
        self._dirty = True

    def _archived_logs(self, session):
        if "logs" in session:
            return session["logs"]
        if self._store is None:
            return []
        record = self._store.load(session["run_id"]) or {}
        return record.get("logs") or []

    # ---------- live run ----------
    def _log_callback(self, entry):
        safe_entry = json.loads(json.dumps(entry, ensure_ascii=False, default=str))
        with self._lock:
            self._sequence += 1
            self.logs.append({**safe_entry, "sequence": self._sequence})
            self._mark_dirty()
            data = safe_entry.get("data") or {}
            if isinstance(data.get("attempt"), int):
                self.current_step = data["attempt"]
            event_phases = {
                "environment_info": "preparing",
                "start_run": "initializing",
                "config_snapshot": "configuring",
                "planner_request": "thinking",
                "generation_result": "interpreting",
                "generation_error": "repairing",
                "planner_output": "interpreting",
                "proposed_commands": "command_ready",
                "premature_completion": "repairing",
                "no_commands": "repairing",
                "duplicate_command": "repairing",
                "policy_decision": "policy_review",
                "policy_block": "replanning",
                "approval_decision": "dispatching",
                "execution_result": "result_received",
                "observation": "observing",
                "run_complete": "complete",
                "run_terminated": "complete",
            }
            event = safe_entry.get("event")
            if event == "command_dispatch":
                self.phase = (
                    "awaiting_domain"
                    if data.get("destination") == "remote_handler"
                    else "executing"
                )
            elif event in event_phases:
                self.phase = event_phases[event]
            self.last_activity_at = safe_entry.get("ts") or datetime.now(timezone.utc).isoformat()

    def _web_approver(self, commands):
        with self._lock:
            if self._stop_event.is_set():
                return [], commands, True, None
            self.pending_commands = commands.copy()
            self.privileged_indices = [
                i
                for i, cmd in enumerate(commands)
                if config.REQUIRE_SUPERVISOR_APPROVAL
                and policy_engine.is_privesc_command(cmd)
            ]
            self.proposal_id = uuid.uuid4().hex
            self.status = AgentStatus.WAITING_FOR_USER
            self.phase = "awaiting_approval"
            self.last_activity_at = datetime.now(timezone.utc).isoformat()
            self._approval_event.clear()
            self._mark_dirty()
        while not self._approval_event.wait(0.1):
            if (
                self._stop_event.is_set()
                or policy_engine.check_kill_switch()
                or (self._deadline and time.monotonic() >= self._deadline)
            ):
                self._stop_event.set()
                break
        with self._lock:
            response = self.user_response
            self.user_response = None
            self.pending_commands = []
            self.privileged_indices = []
            self.proposal_id = None
            if self._stop_event.is_set():
                return [], commands, True, None
            self.status = AgentStatus.RUNNING
            self.phase = "executing"
            self.last_activity_at = datetime.now(timezone.utc).isoformat()
            return response or ([], commands, True, None)

    def _run_agent(self, goal, context, mode):
        add_log_callback(self._log_callback)
        human_gate.set_approver(self._web_approver)
        try:
            reason = agent.run(goal, context, stop_event=self._stop_event, run_id=self.run_id,
                               mode=mode, completion_event=self._completion_event)
            with self._lock:
                self.termination_reason = reason
                failed = reason in FAILED_REASONS
                self.status = AgentStatus.ERROR if failed else AgentStatus.COMPLETED
                self.phase = "error" if failed else "complete"
                if failed:
                    self.error_message = (
                        "Task ended without completing the objective: "
                        + termination_label(reason)[0].lower()
                    )
        except Exception as exc:
            print(
                f"Hephestus run error [{type(exc).__name__}]: {exc}",
                file=sys.stderr,
                flush=True,
            )
            with self._lock:
                self.status = AgentStatus.ERROR
                self.phase = "error"
                self.termination_reason = self.termination_reason or "error"
                self.error_message = (
                    "Run failed. Check the local terminal and configuration."
                )
        finally:
            human_gate.set_approver(None)
            remove_log_callback(self._log_callback)
            with self._lock:
                self.pending_commands = []
                self.privileged_indices = []
                self.proposal_id = None
                self._mark_dirty()
            self.flush()

    def _archive_current_session(self):
        if self.run_id and self.logs:
            record = self._current_record()
            record["event_count"] = len(record["logs"])
            self.sessions.insert(0, record)
            self._write(record)
            del self.sessions[200:]

    @staticmethod
    def _display_title(session):
        """Prefer an operator-set title, otherwise derive one from the goal."""
        title = str(session.get("title") or "").strip()
        if title:
            return title
        goal = str(session.get("goal") or "").replace("\n", " ").strip()
        if not goal:
            return "Untitled session"
        return goal if len(goal) <= 80 else goal[:77].rstrip() + "…"

    def start(self, goal, context="", mode="goal"):
        with self._lock:
            if self.status in (
                AgentStatus.RUNNING,
                AgentStatus.WAITING_FOR_USER,
                AgentStatus.STOPPING,
            ):
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            self._archive_current_session()
            self.status = AgentStatus.RUNNING
            self.logs.clear()
            self.pending_commands = []
            self.user_response = None
            self.error_message = None
            self.proposal_id = None
            self.run_id = uuid.uuid4().hex
            self.termination_reason = None
            self.goal = goal
            self.context = context
            self.title = ""
            self.mode = mode
            self.current_step = 0
            self.phase = "preparing"
            self.created_at = datetime.now(timezone.utc).isoformat()
            self.last_activity_at = self.created_at
            self._stop_event = threading.Event()
            self._completion_event = threading.Event()
            self._approval_event.clear()
            self._deadline = (
                time.monotonic() + config.RUN_TIMEOUT_SECONDS
                if config.RUN_TIMEOUT_SECONDS
                else None
            )
            self._mark_dirty()
            self._thread = threading.Thread(
                target=self._run_agent, args=(goal, context, mode), daemon=True
            )
            self._thread.start()
        return True

    def complete(self):
        with self._lock:
            if self.status not in (AgentStatus.RUNNING, AgentStatus.WAITING_FOR_USER):
                return False
            self._completion_event.set()
            self.status = AgentStatus.STOPPING
            self.phase = "operator_completed"
            self.last_activity_at = datetime.now(timezone.utc).isoformat()
            self.proposal_id = None
            self._approval_event.set()
        return True

    def stop(self):
        with self._lock:
            if self.status not in (
                AgentStatus.RUNNING,
                AgentStatus.WAITING_FOR_USER,
                AgentStatus.STOPPING,
            ):
                return False
            self._stop_event.set()
            self.status = AgentStatus.STOPPING
            self.phase = "stopping"
            self.last_activity_at = datetime.now(timezone.utc).isoformat()
            self.proposal_id = None
            self._approval_event.set()
        return True

    def shutdown(self, timeout=8, unload=True):
        self.stop()
        if unload:
            local_llm.unload_model()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self.flush()
        return thread is None or not thread.is_alive()

    def _respond(self, proposal_id, decision, confirmed_privilege=False):
        with self._lock:
            if (
                self.status != AgentStatus.WAITING_FOR_USER
                or not proposal_id
                or proposal_id != self.proposal_id
                or self.user_response is not None
            ):
                return False
            indices, abort, guidance = decision
            if any(
                type(i) is not int or i < 0 or i >= len(self.pending_commands)
                for i in indices
            ):
                return False
            if (
                any(i in self.privileged_indices for i in indices)
                and not confirmed_privilege
            ):
                return False
            selected = set(indices)
            self.user_response = human_gate.ApprovalDecision(
                tuple(self.pending_commands),
                tuple(cmd for i, cmd in enumerate(self.pending_commands) if i in selected),
                tuple(
                    cmd
                    for i, cmd in enumerate(self.pending_commands)
                    if i not in selected
                ),
                abort,
                guidance,
            )
            self.proposal_id = None
            self.status = AgentStatus.RUNNING
            self.phase = "executing"
            self.last_activity_at = datetime.now(timezone.utc).isoformat()
            self._approval_event.set()
        return True

    def edit_pending(self, proposal_id, index, instruction=""):
        """Apply the operator's surgical edit to one pending command, in place.

        The approval modal's "Update" action: the operator says what to change
        (e.g. "change the message to hello") and the model edits only that. The
        run thread stays parked on the approval event; only the pending proposal
        changes, so the operator then reviews and approves (or edits again) it.
        """
        from hephestus.src import planner, safety_review
        with self._lock:
            if (self.status != AgentStatus.WAITING_FOR_USER or not proposal_id
                    or proposal_id != self.proposal_id):
                return None
            if not isinstance(index, int) or not 0 <= index < len(self.pending_commands):
                return None
            original = self.pending_commands[index]
            goal, scope = self.goal, self.context
        variant = planner.edit_command(original, instruction, goal, scope,
                                       previous_variants=[original], mode=self.mode)
        if not variant:
            return None
        if not policy_engine.check_policy(variant)[0]:
            return None
        if config.SAFETY_REVIEW:
            verdict = safety_review.review_command(
                variant, scope, goal=goal, cancel_event=self._stop_event
            )
            self._log_callback({'event': 'safety_review', 'data': {
                'command': variant, 'allowed': verdict['allowed'],
                'reason': verdict['reason'], 'source': verdict['source'],
            }})
            if not verdict['allowed']:
                return None
        with self._lock:
            # The proposal may have been resolved or replaced while we generated.
            if (self.status != AgentStatus.WAITING_FOR_USER or proposal_id != self.proposal_id
                    or index >= len(self.pending_commands) or self.pending_commands[index] != original):
                return None
            self.pending_commands[index] = variant
            # Any view still displaying the old text loses its approval capability.
            self.proposal_id = uuid.uuid4().hex
            self.privileged_indices = [
                i for i, cmd in enumerate(self.pending_commands)
                if config.REQUIRE_SUPERVISOR_APPROVAL and policy_engine.is_privesc_command(cmd)
            ]
            self.last_activity_at = datetime.now(timezone.utc).isoformat()
            self._mark_dirty()
        self._log_callback({'event': 'edited_command', 'data': {
            'original': original, 'variant': variant, 'instruction': instruction,
        }})
        return variant

    def approve(self, approved_indices, proposal_id=None, confirmed_privilege=False):
        return self._respond(
            proposal_id, (approved_indices, False, None), confirmed_privilege
        )

    def reject_all(self, proposal_id=None):
        return self._respond(proposal_id, ([], False, None))

    def abort(self, proposal_id=None):
        return self._respond(proposal_id, ([], True, None))

    def update_goal(self, new_instructions, proposal_id=None):
        return self._respond(proposal_id, ([], False, new_instructions))

    def get_state(self):
        with self._lock:
            label, detail = termination_label(self.termination_reason)
            return dict(
                status=self.status.value,
                pending_commands=self.pending_commands.copy(),
                proposal_id=self.proposal_id,
                privileged_indices=self.privileged_indices.copy(),
                run_id=self.run_id,
                termination_reason=self.termination_reason,
                termination_label=label,
                termination_detail=detail,
                error_message=self.error_message,
                log_count=len(self.logs),
                log_sequence=self._sequence,
                goal=self.goal,
                title=self._display_title({"title": self.title, "goal": self.goal}),
                mode=self.mode,
                created_at=self.created_at,
                progress=self._progress_snapshot(),
            )

    def _progress_snapshot(self):
        stage_count = 10
        stages = {
            "preparing": (1, 5, "Preparing the run"),
            "initializing": (2, 10, "Initializing the local model runtime"),
            "configuring": (3, 14, "Applying the session configuration"),
            "thinking": (4, 24, "Model is planning the next action"),
            "interpreting": (5, 38, "Interpreting the model response"),
            "repairing": (5, 44, "Repairing an incomplete model response"),
            "command_ready": (6, 52, "Command proposal is ready"),
            "policy_review": (6, 58, "Checking command policy"),
            "replanning": (6, 62, "Re-planning after a policy or evidence check"),
            "dispatching": (7, 66, "Preparing command dispatch"),
            "awaiting_approval": (7, 70, "Awaiting operator approval"),
            "awaiting_domain": (7, 72, "Sent to the remote PC — awaiting approval or result"),
            "executing": (8, 78, "Executing the approved command"),
            "result_received": (9, 88, "Command result received"),
            "observing": (9, 94, "Recording and interpreting command evidence"),
            "operator_completed": (10, 96, "Finishing at the operator's request"),
            "stopping": (10, 96, "Stopping after the current operation"),
            "complete": (10, 100, "Run complete — review the output"),
            "error": (10, 100, "Run stopped with an error"),
            "idle": (0, 0, "Ready for a task"),
        }
        if self.status == AgentStatus.COMPLETED:
            stage_index, percent, label = stages["complete"]
            if self.termination_reason in TERMINATION_REASONS:
                label = termination_label(self.termination_reason)[0] + " — review the recorded evidence"
        elif self.status == AgentStatus.ERROR:
            stage_index, percent, label = stages["error"]
            if self.termination_reason in TERMINATION_REASONS:
                label = termination_label(self.termination_reason)[0]
        elif self.status == AgentStatus.IDLE:
            stage_index, percent, label = stages["idle"]
        elif self.status == AgentStatus.STOPPING:
            stage_index, percent, label = stages["stopping"]
        else:
            stage_index, percent, label = stages.get(self.phase, stages["preparing"])
        if self.current_step and self.status in {AgentStatus.RUNNING, AgentStatus.WAITING_FOR_USER}:
            label = f"Iteration {self.current_step} · {label}"
        return {
            "percent": percent,
            "label": label + (" — stage estimate" if 0 < percent < 100 else ""),
            "phase": self.phase,
            "stage_index": stage_index,
            "stage_count": stage_count,
            "last_activity_at": self.last_activity_at,
            "current_step": self.current_step,
            "max_steps": min(config.MAX_STEPS, 3) if self.mode == "quick" else config.MAX_STEPS,
        }

    def get_logs(self, since=0):
        with self._lock:
            return [entry.copy() for entry in self.logs if entry["sequence"] > since]

    @staticmethod
    def _sha256(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _engagement_commands(self, entries):
        """Summarize the operator-visible proposal, approval and outcome evidence."""
        records = {}
        order = []

        def record(attempt, command):
            key = (attempt, command)
            if key not in records:
                records[key] = {
                    "attempt": attempt,
                    "command": command,
                    "command_sha256": self._sha256(command),
                }
                order.append(key)
            return records[key]

        for entry in entries:
            data = entry.get("data") or {}
            attempt = data.get("attempt")
            event = entry.get("event")
            if event == "proposed_commands":
                for command in data.get("commands") or []:
                    if isinstance(command, str):
                        record(attempt, command)["proposed_at"] = entry.get("ts")
            elif event == "policy_decision" and isinstance(data.get("command"), str):
                item = record(attempt, data["command"])
                item["policy"] = {"allowed": bool(data.get("allowed")), "reason": data.get("reason", "")}
            elif event == "approval_decision":
                forwarded = data.get("approval_source") == "domain_pc"
                for command in data.get("approved") or []:
                    if isinstance(command, str):
                        item = record(attempt, command)
                        item["approval"] = "forwarded_to_domain_pc" if forwarded else "approved"
                        item["operator_action"] = ("Forwarded to the remote PC for Y/N/Q approval" if forwarded
                                                   else "Approved in the workspace")
                for command in data.get("rejected") or []:
                    if isinstance(command, str):
                        item = record(attempt, command)
                        if data.get("aborted"):
                            item["approval"] = "aborted"
                            item["operator_action"] = "Task aborted in the workspace approval dialog"
                        else:
                            item["approval"] = "rejected"
                            item["operator_action"] = "Rejected in the workspace approval dialog"
            elif event == "execution_result" and isinstance(data.get("command"), str):
                item = record(attempt, data["command"])
                stdout = str(data.get("stdout", ""))
                stderr = str(data.get("stderr", ""))
                remote = data.get("remote_status")
                item["executed_at"] = entry.get("ts")
                item["execution"] = {
                    "exit_code": data.get("exit_code"),
                    "executed": data.get("executed"),
                    "execution_uncertain": bool(data.get("execution_uncertain")),
                    "job_id": data.get("job_id"),
                    "remote_status": remote,
                    "stdout_sha256": self._sha256(stdout),
                    "stderr_sha256": self._sha256(stderr),
                }
                if data.get("execution_uncertain"):
                    item["operator_action"] = "Remote outcome unknown: the command may have run. Inspect the target before retrying."
                elif remote == "rejected":
                    item["approval"] = "rejected_on_domain_pc"
                    item["operator_action"] = "Rejected by the remote-PC operator (N)"
                elif remote == "quit":
                    item["approval"] = "worker_quit"
                    item["operator_action"] = "Remote-PC operator quit the worker (Q)"
                elif remote == "approved":
                    item["approval"] = "approved_on_domain_pc"
                    item["operator_action"] = "Approved by the remote-PC operator (Y) and executed"
                elif item.get("approval") == "forwarded_to_domain_pc" and not data.get("executed"):
                    item["operator_action"] = "Forwarded to the remote PC, but it never ran (relay or worker unavailable)"
        return [records[key] for key in order]

    @staticmethod
    def _operator_decisions(entries):
        """Count every kind of operator decision so exports never conflate them."""
        summary = {
            "approved_in_workspace": 0,
            "rejected_in_workspace": 0,
            "aborted_in_workspace": 0,
            "guidance_updates": 0,
            "approved_on_domain_pc": 0,
            "rejected_on_domain_pc": 0,
            "worker_quit_on_domain_pc": 0,
        }
        for entry in entries:
            data = entry.get("data") or {}
            if entry.get("event") == "approval_decision":
                if data.get("approval_source") != "domain_pc":
                    summary["approved_in_workspace"] += len(data.get("approved") or [])
                if data.get("aborted"):
                    summary["aborted_in_workspace"] += 1
                else:
                    summary["rejected_in_workspace"] += len(data.get("rejected") or [])
                if data.get("goal_update_provided"):
                    summary["guidance_updates"] += 1
            elif entry.get("event") == "execution_result":
                remote = data.get("remote_status")
                if remote == "approved":
                    summary["approved_on_domain_pc"] += 1
                elif remote == "rejected":
                    summary["rejected_on_domain_pc"] += 1
                elif remote == "quit":
                    summary["worker_quit_on_domain_pc"] += 1
        return summary

    def _find_archived(self, run_id):
        for session in self.sessions:
            if session["run_id"] == run_id:
                return session
        return None

    def _resolve_session(self, run_id):
        """Return (metadata, logs) for the current or an archived session, else None."""
        if run_id is None or run_id == self.run_id:
            if not self.run_id:
                return None, None
            meta = {
                "run_id": self.run_id,
                "goal": self.goal,
                "context": self.context,
                "title": self._display_title({"title": self.title, "goal": self.goal}),
                "mode": self.mode,
                "status": self.status.value,
                "termination_reason": self.termination_reason,
                "created_at": self.created_at,
                "state": self.get_state(),
            }
            return meta, list(self.logs)
        session = self._find_archived(run_id)
        if session is None:
            return None, None
        meta = {
            "run_id": session["run_id"],
            "goal": session.get("goal", ""),
            "context": session.get("context", ""),
            "title": self._display_title(session),
            "mode": session.get("mode"),
            "status": session.get("status"),
            "termination_reason": session.get("termination_reason"),
            "created_at": session.get("created_at"),
            "state": {key: session[key] for key in session if key not in {"logs", "persisted"}},
        }
        return meta, self._archived_logs(session)

    def export_snapshot(self, run_id=None):
        """Build an explicit, self-describing engagement handoff."""
        with self._lock:
            meta, raw_logs = self._resolve_session(run_id)
            if meta is None:
                raise RuntimeError("session not found")
            entries = json.loads(
                json.dumps(
                    [entry.copy() for entry in raw_logs],
                    ensure_ascii=False,
                    default=str,
                )
            )
            state = meta["state"]
            previous_hash = "0" * 64
            timeline = []
            for entry in entries:
                canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
                event_hash = self._sha256(previous_hash + canonical)
                timeline.append({**entry, "integrity": {"previous_sha256": previous_hash, "event_sha256": event_hash}})
                previous_hash = event_hash
            canonical_timeline = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
            started_at = entries[0].get("ts") if entries else None
            ended_at = entries[-1].get("ts") if entries else None
            recorded_config = next(
                (
                    entry.get("data")
                    for entry in entries
                    if entry.get("event") == "config_snapshot"
                ),
                None,
            )
            source_config = recorded_config or config.snapshot()
            label, detail = termination_label(meta["termination_reason"])
            return {
                "schema": "hephestus.engagement-handoff/v1",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "integrity": {
                    "algorithm": "sha256",
                    "timeline_sha256": self._sha256(canonical_timeline),
                    "final_event_sha256": previous_hash,
                    "scope": "Hashes verify this exported timeline after generation; they do not independently attest to host identity or command execution.",
                },
                "session": {
                    "run_id": meta["run_id"],
                    "title": meta["title"],
                    "goal": meta["goal"],
                    "context": meta["context"],
                    "mode": meta["mode"],
                    "status": meta["status"],
                    "termination_reason": meta["termination_reason"],
                    "termination_label": label,
                    "termination_detail": detail,
                    "created_at": meta["created_at"],
                    "started_at": started_at,
                    "ended_at": ended_at,
                },
                "operator_decisions": self._operator_decisions(entries),
                "configuration": {
                    "backend": source_config.get("backend"),
                    "model_file": str(source_config.get("llm_model", ""))
                    .rsplit("/", 1)[-1]
                    .rsplit("\\", 1)[-1],
                    "runtime_settings": {
                        name: source_config.get(name)
                        for name in config.RUNTIME_CONTROL_ALIASES
                    },
                },
                "state": state,
                "commands": self._engagement_commands(entries),
                "timeline": timeline,
            }

    # ---------- read-only questions about a session ----------
    def get_log_context(self, question=""):
        """A bounded transcript of the current session for read-only operator questions."""
        return self.get_session_log_context(self.run_id, question) or "SESSION: (no session has run yet)"

    @staticmethod
    def _clip_context_text(value, limit):
        text = str(value)
        if len(text) <= limit:
            return text
        head = max(1, int(limit * 0.65))
        tail = max(1, limit - head - 48)
        return text[:head] + "\n… [content compacted for local model] …\n" + text[-tail:]

    @staticmethod
    def _question_terms(question):
        words = re.findall(r"[a-z0-9_.:\\/$-]{3,}", str(question).lower())
        return {w.strip(".:-") for w in words if w.strip(".:-") not in _STOPWORDS and len(w.strip(".:-")) >= 3}

    @classmethod
    def _excerpt(cls, text, terms, limit):
        """Keep the lines of long output that mention the question's terms, plus head and tail."""
        text = str(text or "")
        if len(text) <= limit:
            return text
        lines = text.splitlines()
        keep = set(range(min(8, len(lines)))) | set(range(max(0, len(lines) - 12), len(lines)))
        if terms:
            for index, line in enumerate(lines):
                lowered = line.lower()
                if any(term in lowered for term in terms):
                    keep.update(range(max(0, index - 2), min(len(lines), index + 3)))
        chosen, size, previous = [], 0, -1
        for index in sorted(keep):
            line = lines[index][:400]
            if size + len(line) + 1 > limit:
                break
            if previous != -1 and index != previous + 1:
                chosen.append("  […]")
            chosen.append(line)
            size += len(line) + 1
            previous = index
        if len(chosen) <= 2:
            return cls._clip_context_text(text, limit)
        return "\n".join(chosen)

    @classmethod
    def _render_context_event(cls, entry, terms):
        """Compact plain-text rendering (far fewer tokens than JSON); None means skip."""
        event = entry.get("event")
        data = entry.get("data") or {}
        stamp = str(entry.get("ts") or "")[11:19]
        prefix = f"[#{entry.get('sequence', '?')} {stamp}]"
        if event in {"config_snapshot", "environment_info", "planner_request", "command_dispatch",
                     "observation", "policy_block"}:
            return None
        if event == "start_run":
            return f"{prefix} TASK STARTED ({data.get('mode', 'goal')} mode): {cls._clip_context_text(data.get('goal', ''), 800)}"
        if event == "generation_result":
            if not data.get("validation_error"):
                return None
            return f"{prefix} CONTROLLER REJECTED MODEL OUTPUT: {cls._clip_context_text(data['validation_error'], 300)}"
        if event == "generation_error":
            return f"{prefix} MODEL ERROR: {cls._clip_context_text(data.get('message', ''), 300)}"
        if event == "planner_output":
            return f"{prefix} MODEL PLAN:\n{cls._clip_context_text(data.get('text', ''), 700)}"
        if event == "proposed_commands":
            commands = [c for c in data.get("commands") or [] if isinstance(c, str)]
            return f"{prefix} PROPOSED: " + ("; ".join(cls._clip_context_text(c, 500) for c in commands) or "(none)")
        if event == "policy_decision":
            if data.get("allowed"):
                return None
            return f"{prefix} POLICY BLOCKED: {cls._clip_context_text(data.get('command', ''), 300)} — {data.get('reason', '')}"
        if event == "approval_decision":
            parts = [f"approved {len(data.get('approved') or [])}", f"rejected {len(data.get('rejected') or [])}"]
            if data.get("aborted"):
                parts.append("ABORTED the task")
            if data.get("goal_update_provided"):
                parts.append("gave new guidance")
            return f"{prefix} OPERATOR DECISION (workspace): " + ", ".join(parts)
        if event == "execution_result":
            remote = data.get("remote_status")
            status = {"rejected": "REJECTED by remote-PC operator (not executed)",
                      "quit": "remote-PC operator QUIT the worker (not executed)"}.get(remote, "")
            lines = [f"{prefix} COMMAND RESULT exit={data.get('exit_code')} executed={data.get('executed')}"
                     + (f" — {status}" if status else ""),
                     "$ " + cls._clip_context_text(data.get("command", ""), 600)]
            stdout = cls._excerpt(data.get("stdout"), terms, 2200)
            stderr = cls._excerpt(data.get("stderr"), terms, 900)
            if stdout:
                lines.append("stdout:\n" + stdout)
            if stderr:
                lines.append("stderr:\n" + stderr)
            return "\n".join(lines)
        if event in {"run_complete", "run_terminated"}:
            reason = data.get("reason") or data.get("status")
            label, detail = termination_label(reason)
            return f"{prefix} RUN ENDED: {label or reason} — {detail}"
        compact = json.dumps(data, ensure_ascii=False, default=str)
        return f"{prefix} {str(event).replace('_', ' ').upper()}: {cls._clip_context_text(compact, 300)}"

    def get_session_log_context(self, run_id, question=""):
        """Select the events that matter for ``question`` within a small prompt budget.

        Long sessions are not truncated blindly: every event is scored by how
        well it matches the question (plus recency and importance), the best
        ones are kept in chronological order, and the remainder is summarized
        as counts. A smaller prompt is what keeps a large local model fast.
        """
        with self._lock:
            if run_id and run_id == self.run_id:
                entries = [entry.copy() for entry in self.logs]
                info = {"title": self._display_title({"title": self.title, "goal": self.goal}),
                        "status": self.status.value, "termination_reason": self.termination_reason,
                        "mode": self.mode}
            else:
                session = self._find_archived(run_id) if run_id else None
                if session is None:
                    return None
                entries = list(self._archived_logs(session))
                info = {"title": self._display_title(session), "status": session.get("status"),
                        "termination_reason": session.get("termination_reason"), "mode": session.get("mode")}
        terms = self._question_terms(question)
        wants_recent = bool(_RECENCY_WORDS & set(re.findall(r"[a-z]+", str(question).lower())))
        rendered = []
        total = max(1, len(entries))
        for index, entry in enumerate(entries):
            text = self._render_context_event(entry, terms)
            if text is None:
                continue
            event = entry.get("event")
            importance = {"start_run": 1000, "run_complete": 1000, "run_terminated": 1000,
                          "execution_result": 6, "approval_decision": 5, "policy_decision": 4,
                          "proposed_commands": 3, "generation_error": 2, "planner_output": 2}.get(event, 1)
            lowered = text.lower()
            hits = sum(lowered.count(term) for term in terms) if terms else 0
            recency = (index + 1) / total
            score = importance + min(hits, 12) * 4 + recency * (14 if wants_recent else 5)
            rendered.append((score, index, text))

        executed = [e for e in entries if e.get("event") == "execution_result"]
        succeeded = sum(1 for e in executed if (e.get("data") or {}).get("exit_code") == 0
                        and (e.get("data") or {}).get("executed"))
        label, _detail = termination_label(info["termination_reason"])
        header = (
            f"SESSION: {info['title']} | mode {info['mode']} | status {info['status']}"
            + (f" | ended: {label}" if label else "")
            + f"\nTOTALS: {len(entries)} events, {len(executed)} command results "
              f"({succeeded} succeeded, {len(executed) - succeeded} failed or not executed)"
        )
        budget = max(3000, min(config.MAX_CONTEXT_TOKENS, 3500) * 3) - len(header)
        chosen, used = [], 0
        for score, index, text in sorted(rendered, key=lambda item: (-item[0], -item[1])):
            cost = len(text) + 2
            if used + cost > budget:
                if cost > 1200 and used + 1200 <= budget:
                    text = self._clip_context_text(text, 1100)
                    cost = len(text) + 2
                else:
                    continue
            chosen.append((index, text))
            used += cost
        chosen.sort()
        omitted = len(rendered) - len(chosen)
        body = "\n\n".join(text for _, text in chosen)
        if omitted:
            body += f"\n\n({omitted} less relevant events omitted to keep the answer fast.)"
        return header + "\n\n" + body

    # ---------- session list ----------
    def get_sessions(self):
        with self._lock:
            label, _ = termination_label(self.termination_reason)
            current = (
                [{
                    "run_id": self.run_id,
                    "title": self._display_title({"title": self.title, "goal": self.goal}),
                    "goal": self.goal,
                    "mode": self.mode,
                    "status": self.status.value,
                    "termination_reason": self.termination_reason,
                    "termination_label": label,
                    "created_at": self.created_at,
                    "event_count": len(self.logs),
                    "active": self.status.value in ACTIVE_STATUSES,
                    "current": True,
                    "saved": self._store is not None,
                }]
                if self.run_id
                else []
            )
            archived = [
                {
                    "run_id": session["run_id"],
                    "title": self._display_title(session),
                    "goal": session.get("goal", ""),
                    "mode": session.get("mode"),
                    "status": session.get("status"),
                    "termination_reason": session.get("termination_reason"),
                    "termination_label": termination_label(session.get("termination_reason"))[0],
                    "created_at": session.get("created_at"),
                    "event_count": session.get("event_count", len(session.get("logs") or [])),
                    "active": False,
                    "current": False,
                    "saved": self._store is not None,
                }
                for session in self.sessions
            ]
            return current + archived

    def set_session_title(self, run_id, title):
        """Rename the current or an archived session. Empty title restores the default."""
        clean = str(title or "").strip()[:120]
        with self._lock:
            if run_id and run_id == self.run_id:
                self.title = clean
                self._mark_dirty()
                record = None
            else:
                session = self._find_archived(run_id)
                if session is None:
                    return False
                session["title"] = clean
                record = {k: v for k, v in session.items() if k not in {"persisted", "event_count"}}
                record["logs"] = self._archived_logs(session)
        if record is not None:
            self._write(record)
        else:
            self.flush()
        return True

    def delete_session(self, run_id):
        """Wipe a session, its logs and its saved file. An active run must be stopped first."""
        with self._lock:
            from hephestus.src.command_validation import powershell_syntax_error
            powershell_syntax_error.cache_clear()
            if run_id and run_id == self.run_id:
                if self.status.value in ACTIVE_STATUSES:
                    return False
                self.logs.clear()
                self.run_id = None
                self.goal = ""
                self.context = ""
                self.title = ""
                self.created_at = None
                self.termination_reason = None
                self.status = AgentStatus.IDLE
                self.phase = "idle"
                self.current_step = 0
                self.error_message = None
                self.pending_commands = []
                self.proposal_id = None
                self.privileged_indices = []
                self.user_response = None
                self.last_activity_at = None
                self._dirty = False
            else:
                session = self._find_archived(run_id)
                if session is not None:
                    self.sessions.remove(session)
        if self._store is not None and run_id:
            with self._save_lock:
                self._deleted_ids.add(run_id)
                self._store.delete(run_id)
        return True

    def get_session(self, run_id):
        with self._lock:
            if run_id == self.run_id:
                return {"state": self.get_state(), "logs": [entry.copy() for entry in self.logs]}
            session = self._find_archived(run_id)
            if session is None:
                return None
            state = {key: value for key, value in session.items() if key not in {"logs", "persisted"}}
            label, detail = termination_label(session.get("termination_reason"))
            state.update(termination_label=label, termination_detail=detail)
            return {"state": state, "logs": [entry.copy() for entry in self._archived_logs(session)]}


_runner = AgentRunner(storage_dir=sessions_dir())


def get_runner():
    return _runner
