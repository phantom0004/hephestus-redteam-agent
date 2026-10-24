"""Local and SSH command execution after policy and operator approval."""

import os
import signal
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod

from hephestus import config


class Backend(ABC):
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
            args = [powershell, "-NoProfile", "-NonInteractive", "-Command", command]
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


class SSHBackend(Backend):
    """Persistent SSH connection with verified server keys and lazy connection."""

    def __init__(
        self, host=None, port=None, username=None, key_path=None, timeout=None
    ):
        self.host = host or config.SSH_HOST
        self.port = port or config.SSH_PORT
        self.username = username or config.SSH_USERNAME
        self.key_path = key_path or config.SSH_KEY_PATH
        self.ssh_timeout = timeout or config.SSH_TIMEOUT_SECONDS
        self._client = None
        self._command_deadline = None

    def _remaining(self):
        remaining = _timeout()
        if self._command_deadline is not None:
            remaining = min(remaining, self._command_deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("SSH deadline expired before dispatch")
        return remaining

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def _connect(self):
        import paramiko

        if not self.host or not self.username:
            raise ValueError("SSH host and username must be configured")
        self.close()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if config.SSH_KNOWN_HOSTS:
            client.load_host_keys(config.SSH_KNOWN_HOSTS)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            if _stopped():
                raise InterruptedError("SSH connection cancelled")
            # Connection, banner and authentication each receive part of the remaining budget.
            budget = min(self.ssh_timeout, self._remaining()) / 3
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                key_filename=self.key_path or None,
                timeout=budget,
                auth_timeout=budget,
                banner_timeout=budget,
                allow_agent=True,
                look_for_keys=True,
            )
        except Exception:
            client.close()
            raise
        self._client = client

    def _ensure_connected(self):
        transport = self._client.get_transport() if self._client else None
        if transport is None or not transport.is_active():
            self._connect()

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
        channel = None
        dispatched = False
        output, errors = bytearray(), bytearray()
        try:
            end = time.monotonic() + _timeout()
            self._command_deadline = end
            self._ensure_connected()
            if _stopped() or time.monotonic() >= end:
                return _result(
                    command,
                    stderr="Stopped or timed out before SSH dispatch",
                    exit_code=130 if _stopped() else 124,
                    executed=False,
                )
            channel = self._client.get_transport().open_session(
                timeout=min(self.ssh_timeout, self._remaining())
            )
            channel.settimeout(min(self.ssh_timeout, self._remaining()))
            if _stopped() or time.monotonic() >= end:
                return _result(
                    command,
                    stderr="Stopped or timed out before SSH dispatch",
                    exit_code=130 if _stopped() else 124,
                    executed=False,
                )
            dispatched = True
            channel.exec_command(command)
            channel.shutdown_write()
            reason = None
            while True:
                for ready, receive, buffer in (
                    (channel.recv_ready, channel.recv, output),
                    (channel.recv_stderr_ready, channel.recv_stderr, errors),
                ):
                    if ready():
                        buffer.extend(receive(8192))
                if len(output) + len(errors) > config.OUTPUT_MAX_CHARS * 4:
                    reason = "Output limit reached"
                    break
                if _stopped():
                    reason = "Stopped by operator (remote process termination is not guaranteed)"
                    break
                if time.monotonic() >= end:
                    reason = "Execution timed out (remote process termination is not guaranteed)"
                    break
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                time.sleep(0.05)
            code = (
                (130 if reason and reason.startswith("Stopped") else 124)
                if reason
                else channel.recv_exit_status()
            )
            stdout = output.decode("utf-8", errors="replace")
            stderr = errors.decode("utf-8", errors="replace")
            truncated = (
                len(stdout) > config.OUTPUT_MAX_CHARS
                or len(stderr) > config.OUTPUT_MAX_CHARS
            )
            if reason:
                stderr = stderr[: config.OUTPUT_MAX_CHARS] + "\n" + reason
            return _result(
                command,
                stdout[: config.OUTPUT_MAX_CHARS],
                stderr[: config.OUTPUT_MAX_CHARS + 200],
                code,
                output_truncated=truncated,
            )
        except Exception as exc:
            return _result(
                command,
                stderr=f"SSH execution error: {exc}",
                exit_code=124 if isinstance(exc, TimeoutError) else -1,
                executed=dispatched,
            )
        finally:
            self._command_deadline = None
            if channel is not None:
                channel.close()


_backend_instance = LocalBackend()
_backend_registry = {"local": LocalBackend, "ssh": SSHBackend}
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


def close_backend():
    _backend_instance.close()
