import hashlib
import os
import threading
import uuid

UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def hash_bytes(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def make_object_key(submission_id: int) -> str:
    """由持久化 submission id 派生稳定 object key，重试不会再生成新的 UUID。"""
    return f"submission-{int(submission_id)}"


def _path_for_key(key: str) -> str:
    # key 只允许是单层文件名，避免路径穿越；S3/MinIO 接入时这里换成 bucket/key 语义。
    safe = os.path.basename(key)
    if safe != key or not safe:
        raise ValueError("非法 object key")
    return os.path.join(UPLOAD_DIR, safe)


def object_path(key: str) -> str:
    return _path_for_key(key)


def object_exists(key: str) -> bool:
    return os.path.isfile(_path_for_key(key))


def object_hash(key: str) -> str | None:
    path = _path_for_key(key)
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def put_object(file_bytes: bytes, filename: str, key: str | None = None) -> dict:
    # ===== 这里就是“对象存储 (S3 / MinIO)”的接入点 =====
    # 演示用本地磁盘实现；接 MinIO/S3 时，把下面“临时文件 + 原子 replace”换成
    # 对应 SDK 的稳定 Key 上传/覆盖语义。上层只依赖 key/path/hash。
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"文件格式不支持，仅允许 {sorted(ALLOWED_EXT)}")

    # 兼容旧调用：未指定 key 时仍生成随机 key；
    # /api/submit 会显式传 make_object_key(submission_id)，因此重试使用同一个对象。
    object_key = key or (uuid.uuid4().hex + ext)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = _path_for_key(object_key)

    # 先写临时文件，fsync 后再原子替换最终路径。
    # 这样另一个 API 实例只会看到“完整最终对象”或“还没有最终对象”，不会读到半文件。
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(file_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

    return {"key": object_key, "path": path, "hash": hash_bytes(file_bytes)}
