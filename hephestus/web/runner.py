"""One supervised worker and one-use approval proposals per process."""

import threading
import time
import uuid
from collections import deque
from enum import Enum

from hephestus import config
from hephestus.src import agent, human_gate, policy_engine
from hephestus.src.logger import add_log_callback, remove_log_callback


class AgentStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"


class AgentRunner:
    def __init__(self, max_logs=500):
        self.status = AgentStatus.IDLE
        self.logs = deque(maxlen=max_logs)
        self.pending_commands = []
        self.user_response = None
        self.error_message = None
        self.proposal_id = None
        self.privileged_indices = []
        self.run_id = None
        self.termination_reason = None
        self._lock = threading.Lock()
        self._approval_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread = None
        self._sequence = 0
        self._deadline = None

    def _log_callback(self, entry):
        with self._lock:
            self._sequence += 1
            self.logs.append({**entry, "sequence": self._sequence})

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
            self._approval_event.clear()
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
            return response or ([], commands, True, None)

    def _run_agent(self, goal, context):
        add_log_callback(self._log_callback)
        human_gate.set_approver(self._web_approver)
        try:
            reason = agent.run(
                goal, context, stop_event=self._stop_event, run_id=self.run_id
            )
            with self._lock:
                self.termination_reason = reason
                self.status = AgentStatus.COMPLETED
        except Exception:
            with self._lock:
                self.status = AgentStatus.ERROR
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

    def start(self, goal, context=""):
        with self._lock:
            if self.status in (
                AgentStatus.RUNNING,
                AgentStatus.WAITING_FOR_USER,
                AgentStatus.STOPPING,
            ):
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            self.status = AgentStatus.RUNNING
            self.logs.clear()
            self.pending_commands = []
            self.user_response = None
            self.error_message = None
            self.proposal_id = None
            self.run_id = uuid.uuid4().hex
            self.termination_reason = None
            self._stop_event = threading.Event()
            self._approval_event.clear()
            self._deadline = (
                time.monotonic() + config.RUN_TIMEOUT_SECONDS
                if config.RUN_TIMEOUT_SECONDS
                else None
            )
            self._thread = threading.Thread(
                target=self._run_agent, args=(goal, context), daemon=True
            )
            self._thread.start()
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
            self.proposal_id = None
            self._approval_event.set()
        return True

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
            self.user_response = (
                [cmd for i, cmd in enumerate(self.pending_commands) if i in selected],
                [
                    cmd
                    for i, cmd in enumerate(self.pending_commands)
                    if i not in selected
                ],
                abort,
                guidance,
            )
            self.proposal_id = None
            self.status = AgentStatus.RUNNING
            self._approval_event.set()
        return True

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
            return dict(
                status=self.status.value,
                pending_commands=self.pending_commands.copy(),
                proposal_id=self.proposal_id,
                privileged_indices=self.privileged_indices.copy(),
                run_id=self.run_id,
                termination_reason=self.termination_reason,
                error_message=self.error_message,
                log_count=len(self.logs),
                log_sequence=self._sequence,
            )

    def get_logs(self, since=0):
        with self._lock:
            return [entry.copy() for entry in self.logs if entry["sequence"] > since]


_runner = AgentRunner()


def get_runner():
    return _runner
