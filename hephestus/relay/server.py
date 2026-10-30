"""Bounded loopback WSGI server shared by managed and standalone relays."""
import threading
from werkzeug.serving import ThreadedWSGIServer


class BoundedRelayServer(ThreadedWSGIServer):
    """Small fixed request pool so public tunnel traffic cannot spawn unlimited threads."""

    daemon_threads = True
    request_queue_size = 16
    max_concurrent_requests = 8

    def __init__(self, host, port, app):
        self._request_slots = threading.BoundedSemaphore(self.max_concurrent_requests)
        super().__init__(host, port, app)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        return request, address

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


