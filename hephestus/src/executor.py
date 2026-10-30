"""Local or encrypted relay execution after policy and operator approval."""

import base64
import os
import signal
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod

from hephestus import config


class Backend(ABC):
    requires_local_approval = True

    @abstractmethod
    def execute(self, command):
        raise NotImplementedError

    def close(self):
        pass


def _result(command, stdout="", stderr="", exit_code=0, executed=True, **extra):
    return dict(
        command=command,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        executed=executed,
        **extra,
    )


def _stopped():
    from hephestus.src.policy_engine import check_kill_switch

    return (_stop_event is not None and _stop_event.is_set()) or check_kill_switch()


def _timeout():
    value = float(config.COMMAND_TIMEOUT_SECONDS)
    if _deadline is not None:
        value = min(value, max(0, _deadline - time.monotonic()))
    return value


def _kill_process_tree(proc):
    if os.name == "nt":
        taskkill = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"
        )
        subprocess.run(
            [taskkill, "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def _read_output(handle):
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    raw = handle.read(config.OUTPUT_MAX_CHARS * 4)
    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > config.OUTPUT_MAX_CHARS or size > len(raw)
    return text[: config.OUTPUT_MAX_CHARS], truncated


class LocalBackend(Backend):
    """PowerShell on Windows, /bin/sh on POSIX; runs with the current user's rights."""

    def execute(self, command):
        if config.DRY_RUN:
            return _result(command, stderr="dry_run: not executed", executed=False)
        if _stopped() or _timeout() <= 0:
            return _result(
                command,
                stderr="Stopped before execution",
                exit_code=130,
                executed=False,
            )
        shell = os.name != "nt"
        args = command
        if not shell:
            powershell = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "powershell.exe",
            )
            # Passing a script through "-Command" lets Windows argument quoting
            # strip embedded double quotes and split multi-line scripts. Encode
            # the exact text instead, with the same status trailer the domain
            # worker uses so a failing cmdlet yields a non-zero exit code.
            script = (
                '$ErrorActionPreference="Stop"; $LASTEXITCODE=0;' + command +
                '\n$commandSucceeded=$?; if (-not $commandSucceeded) { exit 1 }; exit [int]$LASTEXITCODE'
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            args = [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        proc = None
        try:
            # Disk-backed temporary streams prevent large output from exhausting RAM.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                proc = subprocess.Popen(
                    args,
                    shell=shell,
                    stdout=out,
                    stderr=err,
                    stdin=subprocess.DEVNULL,
                    **options,
                )
                end = time.monotonic() + _timeout()
                reason = None
                while True:
                    if _stopped():
                        reason = "Stopped by operator"
                        break
                    if time.monotonic() >= end:
                        reason = "Execution timed out"
                        break
                    # Limit disk growth as well as the retained text size.
                    if (
                        os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size
                        > config.OUTPUT_MAX_CHARS * 8
                    ):
                        reason = "Output limit reached"
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                if reason:
                    _kill_process_tree(proc)
                stdout, out_cut = _read_output(out)
                stderr, err_cut = _read_output(err)
                if reason:
                    stderr += ("\n" if stderr else "") + reason
                code = (
                    (130 if reason == "Stopped by operator" else 124)
                    if reason
                    else proc.returncode
                )
                return _result(
                    command, stdout, stderr, code, output_truncated=out_cut or err_cut
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return _result(
                command, stderr=f"Local execution error: {exc}", exit_code=-1
            )
        finally:
            # Ctrl+C and unexpected errors must not leave an independent child tree.
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc)


class RelayBackend(Backend):
    """Encrypted command dispatch to the operator-approved remote worker."""

    requires_local_approval = False

    def __init__(
        self,
        url=None,
        master_key=None,
        request_timeout=None,
        result_timeout=None,
        result_poll_interval=None,
        job_ttl=None,
    ):
        from hephestus.relay.client import RelayControllerClient
        from hephestus.relay.secrets import load_key_file

        key = master_key or load_key_file(config.RELAY_MASTER_KEY_PATH)
        self._client = RelayControllerClient(
            url or config.RELAY_URL,
            key,
            request_timeout=request_timeout or config.RELAY_REQUEST_TIMEOUT_SECONDS,
            result_timeout=result_timeout or (config.COMMAND_TIMEOUT_SECONDS + 30),
            result_poll_interval=(
                result_poll_interval
                if result_poll_interval is not None
                else config.RELAY_RESULT_POLL_INTERVAL
            ),
            job_ttl=job_ttl or config.RELAY_JOB_TTL_SECONDS,
            max_output_chars=config.OUTPUT_MAX_CHARS,
        )

    def execute(self, command):
        if config.DRY_RUN:
            return _result(command, stderr="dry_run: not dispatched", executed=False)
        if _stopped() or _timeout() <= 0:
            return _result(
                command,
                stderr="Stopped before relay dispatch",
                exit_code=130,
                executed=False,
            )
        return self._client.execute_command(command, cancelled=_stopped)


_backend_instance = LocalBackend()
_backend_registry = {"local": LocalBackend, "relay": RelayBackend}
_stop_event = None
_deadline = None


def set_execution_limits(stop_event=None, deadline=None):
    global _stop_event, _deadline
    _stop_event, _deadline = stop_event, deadline


def set_backend(backend):
    global _backend_instance
    _backend_instance.close()
    _backend_instance = backend


def register_backend(name, factory):
    _backend_registry[name] = factory


def available_backends():
    return dict(_backend_registry)


def set_backend_by_name(name):
    if name not in _backend_registry:
        raise ValueError(f"Unknown backend: {name}")
    set_backend(_backend_registry[name]())


def execute(command):
    return _backend_instance.execute(command)


def requires_local_approval():
    return _backend_instance.requires_local_approval


def close_backend():
    _backend_instance.close()
