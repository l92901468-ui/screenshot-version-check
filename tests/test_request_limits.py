import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import request_limits


class TestRequestLimits(unittest.TestCase):
    def test_content_length_rejects_oversized_body_before_read(self):
        headers = {"Content-Length": str(500 * 1024 * 1024)}
        with self.assertRaises(request_limits.RequestBodyTooLarge):
            request_limits.checked_content_length(headers, 11 * 1024 * 1024)

    def test_content_length_is_required_for_uploads(self):
        with self.assertRaises(request_limits.LengthRequired):
            request_limits.checked_content_length({}, 11 * 1024 * 1024)
        with self.assertRaises(request_limits.LengthRequired):
            request_limits.checked_content_length(
                {"Transfer-Encoding": "chunked"}, 11 * 1024 * 1024
            )

    def test_limited_reader_never_returns_bytes_past_hard_cap(self):
        raw = io.BytesIO(b"a" * 20)
        reader = request_limits.LimitedReader(raw, 10)

        self.assertEqual(reader.read(6), b"a" * 6)
        self.assertEqual(reader.read(4), b"a" * 4)
        with self.assertRaises(request_limits.RequestBodyTooLarge):
            reader.read(1)

    def test_limited_reader_applies_to_readline_too(self):
        raw = io.BytesIO(b"1234567890\nrest")
        reader = request_limits.LimitedReader(raw, 8)
        with self.assertRaises(request_limits.RequestBodyTooLarge):
            reader.readline()


if __name__ == "__main__":
    unittest.main(verbosity=2)
