import json
import socket
import threading
from http.server import ThreadingHTTPServer


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on live request-handler threads.

    ThreadingHTTPServer normally starts one thread per accepted connection. That is
    convenient for a demo, but an unbounded connection burst can create thousands
    of threads before route-level rate limits even run. This wrapper acquires a
    slot *before* spawning the handler thread. When no slot is available, it sends
    a small 503 response and closes the socket without creating another thread.
    """

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, *, max_active_requests=64):
        limit = int(max_active_requests)
        if limit <= 0:
            raise ValueError("max_active_requests must be greater than 0")
        self.max_active_requests = limit
        self._request_slots = threading.BoundedSemaphore(limit)
        self._stats_lock = threading.Lock()
        self._active_requests = 0
        self._rejected_requests = 0
        super().__init__(server_address, RequestHandlerClass)

    def _mark_acquired(self):
        with self._stats_lock:
            self._active_requests += 1

    def _release_slot(self):
        with self._stats_lock:
            self._active_requests -= 1
        self._request_slots.release()

    def concurrency_stats(self):
        with self._stats_lock:
            return {
                "active": self._active_requests,
                "max_active": self.max_active_requests,
                "rejected": self._rejected_requests,
            }

    def _reject_overload(self, request):
        with self._stats_lock:
            self._rejected_requests += 1

        body = json.dumps(
            {"ok": False, "action": "wait", "msg": "API instance is busy; retry later"}
        ).encode("utf-8")
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Retry-After: 1\r\n"
            + b"Connection: close\r\n\r\n"
            + body
        )

        try:
            # Do not let a client that is not reading responses block the accept loop.
            request.settimeout(0.05)
            request.sendall(response)
        except (OSError, socket.timeout):
            pass
        finally:
            self.shutdown_request(request)

    def process_request(self, request, client_address):
        # Critical point: acquire BEFORE ThreadingMixIn creates a new handler thread.
        if not self._request_slots.acquire(blocking=False):
            self._reject_overload(request)
            return

        self._mark_acquired()
        try:
            super().process_request(request, client_address)
        except Exception:
            # Thread creation itself failed, so no process_request_thread() will
            # exist to release the slot.
            self._release_slot()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_slot()
