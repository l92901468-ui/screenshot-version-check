import socket
import unittest
from http.server import BaseHTTPRequestHandler

from bounded_http import BoundedThreadingHTTPServer


class NoopHandler(BaseHTTPRequestHandler):
    def handle(self):
        pass

    def log_message(self, *args):
        pass


class TestBoundedThreadingHTTPServer(unittest.TestCase):
    def setUp(self):
        self.server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), NoopHandler, max_active_requests=1
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
