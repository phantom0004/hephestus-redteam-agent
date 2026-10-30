"""Best-effort local GPU telemetry with no persistence or shell evaluation."""

import shutil
import subprocess
import threading
import time
from collections import deque


_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu"
_ARGUMENTS = [f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"]
# The live sampler also reads power and memory-controller load for the graph.
_LIVE_QUERY = (
    "utilization.gpu,memory.used,memory.total,temperature.gpu,"
    "power.draw,power.limit,utilization.memory,name"
)
SAMPLE_INTERVAL_MS = 500
HISTORY_SECONDS = 90
_IDLE_SHUTDOWN_SECONDS = 30


def _reading(value: str) -> int:
    number = int(value.strip())
    if number < 0:
        raise ValueError("negative GPU reading")
    return number


def _optional_float(value: str):
    try:
        number = float(value.strip())
    except ValueError:
        return None  # "[N/A]" on GPUs that do not report this field
    return number if number >= 0 else None


def gpu_snapshot() -> dict:
    """Return sanitized NVIDIA readings, or an intentionally detail-free unavailable state."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False, "devices": []}
    try:
        result = subprocess.run(
            [executable, *_ARGUMENTS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1.5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return {"available": False, "devices": []}
        devices = []
        for line in result.stdout.splitlines():
            values = [part.strip() for part in line.split(",")]
            if len(values) != 4:
                continue
            utilization, memory_used, memory_total, temperature = map(_reading, values)
            if memory_used > memory_total or utilization > 100 or temperature > 150:
                continue
            devices.append(
                {
                    "utilization": utilization,
                    "memory_used": memory_used,
                    "memory_total": memory_total,
                    "temperature": temperature,
                }
            )
        return {"available": bool(devices), "devices": devices}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"available": False, "devices": []}


class GpuSampler:
    """One persistent ``nvidia-smi --loop-ms`` reader feeding a time-stamped ring buffer.

    A browser poll only ever reads the buffer, so the graph shows every sample
    the driver reported at a fixed cadence instead of whatever happened to be
    current when a (possibly throttled) tab asked. The process runs only while
    someone is watching and stops itself after a quiet period.
    """

    def __init__(self, interval_ms=SAMPLE_INTERVAL_MS, history_seconds=HISTORY_SECONDS):
        self.interval_ms = interval_ms
        self.samples = deque(maxlen=max(10, history_seconds * 1000 // interval_ms))
        self._lock = threading.Lock()
        self._process = None
        self._last_demand = 0.0
        self._available = None
        self._name = ""

    def _parse(self, line):
        values = [part.strip() for part in line.split(",")]
        if len(values) < 8:
            return None
        try:
            utilization, used, total, temperature = map(_reading, values[:4])
        except ValueError:
            return None
        if used > total or utilization > 100 or temperature > 150:
            return None
        self._name = ",".join(values[7:]).strip()[:80]
        return {
            "t": round(time.time() * 1000),
            "utilization": utilization,
            "memory_used": used,
            "memory_total": total,
            "temperature": temperature,
            "power": _optional_float(values[4]),
            "power_limit": _optional_float(values[5]),
            "memory_utilization": _optional_float(values[6]),
        }

    def _reader(self, process):
        try:
            for line in process.stdout:
                sample = self._parse(line)
                if sample is None:
                    continue
                with self._lock:
                    self.samples.append(sample)
                    self._available = True
                if time.monotonic() - self._last_demand > _IDLE_SHUTDOWN_SECONDS:
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            with self._lock:
                if self._process is process:
                    self._process = None

    def _ensure_running(self):
        if self._process is not None and self._process.poll() is None:
            return True
        executable = shutil.which("nvidia-smi")
        if not executable:
            self._available = False
            return False
        try:
            process = subprocess.Popen(
                [executable, f"--query-gpu={_LIVE_QUERY}", "--format=csv,noheader,nounits",
                 f"--loop-ms={self.interval_ms}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self._available = False
            return False
        self._process = process
        threading.Thread(target=self._reader, args=(process,), daemon=True).start()
        return True

    def history(self, since=0):
        """Return the latest reading plus every buffered sample newer than ``since`` (ms)."""
        with self._lock:
            self._last_demand = time.monotonic()
            running = self._ensure_running()
            samples = [s for s in self.samples if s["t"] > since]
            latest = self.samples[-1] if self.samples else None
        if not running and not samples:
            snapshot = gpu_snapshot()
            return {**snapshot, "history": [], "interval_ms": self.interval_ms}
        devices = [latest] if latest else []
        return {
            "available": bool(latest) or self._available is not False,
            "warming_up": latest is None,
            "name": self._name,
            "devices": devices,
            "history": samples,
            "interval_ms": self.interval_ms,
            "window_ms": self.samples.maxlen * self.interval_ms,
        }

    def close(self):
        with self._lock:
            process, self._process = self._process, None
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass


sampler = GpuSampler()
