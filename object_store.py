import hashlib
import os
import threading
import time
import uuid

UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}
TEMP_FILE_MAX_AGE_SEC = float(os.environ.get("TEMP_FILE_MAX_AGE_SEC", "3600"))
TEMP_GC_INTERVAL_SEC = float(os.environ.get("TEMP_GC_INTERVAL_SEC", "300"))

_gc_lock = threading.Lock()
_last_gc_at = 0.0


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


def cleanup_stale_temp_files(max_age_sec: float | None = None, now: float | None = None) -> int:
    """Best-effort 清理 hard kill / 掉电后遗留的旧 .tmp 文件。

    只删除超过 max_age_sec 的临时文件；不会碰稳定 final object。
    返回成功删除的文件数。清理失败不影响主上传流程。
    """
    age = TEMP_FILE_MAX_AGE_SEC if max_age_sec is None else float(max_age_sec)
    now_ts = time.time() if now is None else float(now)
    if age < 0:
        raise ValueError("max_age_sec 不能为负数")
    if not os.path.isdir(UPLOAD_DIR):
        return 0

    removed = 0
    cutoff = now_ts - age
    try:
        names = os.listdir(UPLOAD_DIR)
    except OSError:
        return 0

    for name in names:
        if not name.endswith(".tmp"):
            continue
        path = os.path.join(UPLOAD_DIR, name)
        try:
            # 只处理普通文件，避免误删目录/特殊文件。
            if not os.path.isfile(path):
                continue
            if os.path.getmtime(path) > cutoff:
                continue
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            # 可能被另一个 API 实例先清掉了。
            continue
        except OSError:
            # GC 是 best-effort；权限/瞬时 I/O 错误不能阻塞上传。
            continue
    return removed


def maybe_cleanup_stale_temp_files() -> int:
    """限频执行临时文件 GC，避免每个上传请求都扫描目录。"""
    global _last_gc_at
    now_mono = time.monotonic()
    with _gc_lock:
        if _last_gc_at and now_mono - _last_gc_at < TEMP_GC_INTERVAL_SEC:
            return 0
        _last_gc_at = now_mono
    return cleanup_stale_temp_files()


def put_object(file_bytes: bytes, filename: str, key: str | None = None) -> dict:
    # ===== 这里就是“对象存储 (S3 / MinIO)”的接入点 =====
    # 演示用本地磁盘实现；接 MinIO/S3 时，把下面“临时文件 + 原子 replace”换成
    # 对应 SDK 的稳定 Key 上传/覆盖语义。上层只依赖 key/path/hash。
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"文件格式不支持，仅允许 {sorted(ALLOWED_EXT)}")

    # 顺手清理 hard kill 遗留的旧临时文件；限频执行，且失败不影响本次上传。
    maybe_cleanup_stale_temp_files()

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
