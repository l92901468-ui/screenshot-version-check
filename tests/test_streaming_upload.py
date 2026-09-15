import hashlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import object_store
import validate


class TrackingBytesIO(io.BytesIO):
    """测试用：如果业务代码偷偷 read() 整个文件，直接失败。"""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.max_requested = 0
        self.read_calls = 0

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("streaming path must not call unbounded read()")
        self.read_calls += 1
        self.max_requested = max(self.max_requested, size)
        return super().read(size)


class TestStreamingUpload(unittest.TestCase):
    def setUp(self):
        self._old_upload_dir = object_store.UPLOAD_DIR
        object_store.UPLOAD_DIR = tempfile.mkdtemp()
        object_store._last_gc_at = 0.0

    def tearDown(self):
        object_store.UPLOAD_DIR = self._old_upload_dir
        object_store._last_gc_at = 0.0

    def test_validation_hashes_in_bounded_chunks_and_rewinds(self):
        body = b"\x89PNG" + (b"a" * (3 * 1024 * 1024))
        stream = TrackingBytesIO(body)
        chunk_size = 64 * 1024

        ok, msg, file_hash, size = validate.inspect_file_stream(
            stream, "evidence.png", chunk_size=chunk_size
        )

        self.assertTrue(ok, msg)
        self.assertEqual(size, len(body))
        self.assertEqual(file_hash, hashlib.sha256(body).hexdigest())
        self.assertLessEqual(stream.max_requested, chunk_size)
        self.assertGreater(stream.read_calls, 1)
        self.assertEqual(stream.tell(), 0, "第一遍扫描后必须 rewind，供对象存储第二遍读取")

    def test_object_store_writes_in_bounded_chunks(self):
        body = b"\x89PNG" + (b"b" * (2 * 1024 * 1024))
        stream = TrackingBytesIO(body)
        chunk_size = 32 * 1024
        expected = hashlib.sha256(body).hexdigest()

        meta = object_store.put_object_stream(
            stream,
            "evidence.png",
            key="submission-42",
            expected_hash=expected,
            chunk_size=chunk_size,
        )

        self.assertEqual(meta["hash"], expected)
        self.assertEqual(meta["size"], len(body))
        self.assertLessEqual(stream.max_requested, chunk_size)
        self.assertGreater(stream.read_calls, 1)
        with open(meta["path"], "rb") as f:
            self.assertEqual(f.read(), body)
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(object_store.UPLOAD_DIR)))

    def test_hash_mismatch_never_replaces_existing_final_object(self):
        old_body = b"\x89PNGold"
        key = "submission-7"
        old_meta = object_store.put_object(old_body, "evidence.png", key=key)

        new_body = b"\x89PNGnew"
        wrong_hash = hashlib.sha256(b"different").hexdigest()
        with self.assertRaises(RuntimeError):
            object_store.put_object_stream(
                TrackingBytesIO(new_body),
                "evidence.png",
                key=key,
                expected_hash=wrong_hash,
                chunk_size=4,
            )

        with open(old_meta["path"], "rb") as f:
            self.assertEqual(f.read(), old_body, "hash 不一致时不能覆盖已有 final object")
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(object_store.UPLOAD_DIR)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
