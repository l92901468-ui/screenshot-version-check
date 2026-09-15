import socket
import time
import unittest
from http.server import BaseHTTPRequestHandler

from bounded_http import BoundedThreadingHTTPServer


class NoopHandler(BaseHTTPRequestHandler):
    def handle(self):
        pass

    def log_message(self, *args):
        pass


class BlockingHandler:
    """Test handler that stays blocked until the server closes its socket."""

    def __init__(self, request, client_address, server):
        try:
            request.recv(1)
        except OSError:
            pass


class TestBoundedThreadingHTTPServer(unittest.TestCase):
    def setUp(self):
        self.server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            NoopHandler,
            max_active_requests=1,
            request_idle_timeout_sec=1.0,
            request_hard_timeout_sec=2.0,
        )

    def tearDown(self):
        self.server.server_close()

    def test_rejects_before_spawning_another_handler_thread(self):
        # Occupy the only request slot to simulate one live handler.
        self.assertTrue(self.server._request_slots.acquire(blocking=False))
        self.server._mark_acquired()

        client, server_side = socket.socketpair()
        try:
            self.server.process_request(server_side, ("local", 0))
            response = client.recv(4096)
            self.assertIn(b"503 Service Unavailable", response)
            self.assertIn(b"Retry-After: 1", response)
            stats = self.server.concurrency_stats()
            self.assertEqual(stats["active"], 1)
            self.assertEqual(stats["rejected"], 1)
        finally:
            client.close()
            self.server._release_slot()

    def test_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            BoundedThreadingHTTPServer(("127.0.0.1", 0), NoopHandler, max_active_requests=0)

    def test_timeouts_must_be_positive(self):
        with self.assertRaises(ValueError):
            BoundedThreadingHTTPServer(
                ("127.0.0.1", 0),
                NoopHandler,
                request_idle_timeout_sec=0,
            )
        with self.assertRaises(ValueError):
            BoundedThreadingHTTPServer(
                ("127.0.0.1", 0),
                NoopHandler,
                request_hard_timeout_sec=0,
            )

    def test_hard_timeout_interrupts_stuck_handler_and_releases_slot(self):
        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            BlockingHandler,
            max_active_requests=1,
            request_idle_timeout_sec=1.0,
            request_hard_timeout_sec=0.05,
        )
        client, server_side = socket.socketpair()
        try:
            server.process_request(server_side, ("local", 0))

            deadline = time.time() + 1.0
            stats = server.concurrency_stats()
            while stats["active"] != 0 and time.time() < deadline:
                time.sleep(0.01)
                stats = server.concurrency_stats()

            self.assertEqual(stats["active"], 0)
            self.assertEqual(stats["timed_out"], 1)
            self.assertEqual(stats["idle_timeout_sec"], 1.0)
            self.assertEqual(stats["hard_timeout_sec"], 0.05)
        finally:
            client.close()
            server.server_close()

    def test_finished_request_is_not_counted_as_timeout(self):
        client, server_side = socket.socketpair()
        finished = __import__("threading").Event()
        finished.set()
        try:
            self.server._expire_request(server_side, finished)
            self.assertEqual(self.server.concurrency_stats()["timed_out"], 0)
        finally:
            client.close()
            server_side.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
