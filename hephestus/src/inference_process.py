"""One reusable, disposable inference process; native loading/generation can be stopped."""

import multiprocessing
import threading


class InferenceProcess:
    def __init__(self, worker):
        self.worker = worker
        self.process = None
        self.connection = None
        self._calls = threading.Lock()
        self._state = threading.Lock()

    def is_alive(self):
        with self._state:
            return self.process is not None and self.process.is_alive()

    def close(self):
        with self._state:
            process, connection = self.process, self.connection
            self.process = self.connection = None
            if process is not None:
                if process.is_alive():
                    try:
                        connection.send(None)
                        process.join(0.5)
                    except (OSError, EOFError):
                        pass
                    if process.is_alive():
                        process.terminate()
                process.join(3)
                if process.is_alive():
                    process.kill()
                    process.join(3)
                process.close()
            if connection is not None:
                connection.close()

    def request(self, payload, cancel_event=None, on_partial=None):
        def cancelled():
            return cancel_event is not None and cancel_event.is_set()
        while not self._calls.acquire(timeout=0.1):
            if cancelled():
                raise InterruptedError("Model request cancelled")
        try:
            if cancelled():
                raise InterruptedError("Model request cancelled")
            with self._state:
                if self.process is None:
                    context = multiprocessing.get_context("spawn")
                    parent, child = context.Pipe()
                    self.process = context.Process(target=self.worker, args=(child,), daemon=True)
                    self.connection = parent
                    self.process.start()
                    child.close()
                connection = self.connection
                connection.send(payload)
            while True:
                if cancelled():
                    self.close()
                    raise InterruptedError("Model request cancelled")
                with self._state:
                    if connection is not self.connection:
                        raise InterruptedError("Model process stopped")
                    if connection.poll(0.1):
                        result = connection.recv()
                        if "partial" in result:
                            # Streamed tokens arrive before the final result.
                            if on_partial is not None:
                                on_partial(result["partial"])
                            continue
                        if not result.get("ok"):
                            raise RuntimeError(result.get("error", "Local inference failed"))
                        return result["value"]
                    if not self.process.is_alive():
                        raise RuntimeError("Local inference process exited unexpectedly")
        except (EOFError, BrokenPipeError, OSError, RuntimeError):
            self.close()
            raise
        finally:
            self._calls.release()
