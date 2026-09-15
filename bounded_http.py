import json
import os
import socket
import threading
from http.server import ThreadingHTTPServer


def _positive_timeout(value, env_name: str, default: float) -> float:
    raw = os.environ.get(env_name, str(default)) if value is None else value
    timeout = float(raw)
    if timeout <= 0:
        raise ValueError(f"{env_name} must be greater than 0")
    return timeout


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with bounded handler concurrency and slow-client timeouts.

    There are three independent protections around request handling:
      1. max_active_requests caps live request-handler threads;
      2. request_idle_timeout_sec closes clients that stop making socket progress;
      3. request_hard_timeout_sec force-closes a request even if it trickles bytes often
         enough to stay below the idle timeout.

    The hard timeout is deliberately enforced by a watchdog outside the handler's read
    loop. This matters because a buffered ``readline()`` may keep receiving tiny chunks
    without returning control to application code, so an application-only elapsed-time
    check around ``readline()`` is not sufficient against a trickle client.
    """

    daemon_threads = True

    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        *,
        max_active_requests=64,
        request_idle_timeout_sec=None,
        request_hard_timeout_sec=None,
    ):
        limit = int(max_active_requests)
        if limit <= 0:
            raise ValueError("max_active_requests must be greater than 0")

        self.max_active_requests = limit
        self.request_idle_timeout_sec = _positive_timeout(
            request_idle_timeout_sec,
            "REQUEST_IDLE_TIMEOUT_SEC",
            10.0,
        )
        self.request_hard_timeout_sec = _positive_timeout(
            request_hard_timeout_sec,
            "REQUEST_HARD_TIMEOUT_SEC",
            60.0,
        )

        self._request_slots = threading.BoundedSemaphore(limit)
        self._stats_lock = threading.Lock()
        self._active_requests = 0
        self._rejected_requests = 0
        self._timed_out_requests = 0
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
                "timed_out": self._timed_out_requests,
                "idle_timeout_sec": self.request_idle_timeout_sec,
                "hard_timeout_sec": self.request_hard_timeout_sec,
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

    def _expire_request(self, request, finished):
        """Watchdog callback for the absolute request lifetime."""
        if finished.is_set():
            return

        with self._stats_lock:
            self._timed_out_requests += 1

        try:
            # shutdown() interrupts a handler blocked in recv()/BufferedReader.readline().
            # The normal ThreadingHTTPServer cleanup path closes the socket afterwards.
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def process_request(self, request, client_address):
        # Critical point: acquire BEFORE ThreadingMixIn creates a new handler thread.
        if not self._request_slots.acquire(blocking=False):
            self._reject_overload(request)
            return

        try:
            # Applies to request-line/header reads as well as multipart body reads.
            # A client that sends nothing for this interval is disconnected.
            request.settimeout(self.request_idle_timeout_sec)
        except OSError:
            self._request_slots.release()
            self.shutdown_request(request)
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
        # Idle timeout alone can be defeated by trickling one byte often enough.
        # The watchdog bounds total handler lifetime regardless of continued progress.
        finished = threading.Event()
        watchdog = threading.Timer(
            self.request_hard_timeout_sec,
            self._expire_request,
            args=(request, finished),
        )
        watchdog.daemon = True
        watchdog.start()

        try:
            super().process_request_thread(request, client_address)
        finally:
            finished.set()
            watchdog.cancel()
            self._release_slot()
