import hashlib
import io
import os

MAX_SIZE = 10 * 1024 * 1024  # 10MB 上限
STREAM_CHUNK_SIZE = int(os.environ.get("UPLOAD_CHUNK_SIZE", str(1024 * 1024)))  # 默认 1MiB
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def inspect_file_stream(file_obj, filename: str, chunk_size: int | None = None):
    """分块扫描上传文件，返回 (ok, msg, sha256, size)。

    不把整个文件复制进 Python bytes：内存开销约等于一个 chunk。
    扫描完成后会把可 seek 的 stream 回绕到开头，供对象存储第二遍流式读取。
    """
    size_per_read = STREAM_CHUNK_SIZE if chunk_size is None else int(chunk_size)
    if size_per_read <= 0:
        raise ValueError("chunk_size 必须大于 0")

    h = hashlib.sha256()
    total = 0
    head = b""

    try:
        file_obj.seek(0)
    except (AttributeError, OSError):
        raise ValueError("上传文件流必须支持 seek，才能在校验后继续写对象存储")

    while True:
        chunk = file_obj.read(size_per_read)
        if not chunk:
            break
        total += len(chunk)
        h.update(chunk)
        if len(head) < 4:
            head += chunk[: 4 - len(head)]

    file_hash = h.hexdigest()

    # cgi.FieldStorage 的上传文件是可回绕的临时文件；第二遍由 object_store 流式写出。
    file_obj.seek(0)

    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return False, f"文件格式不支持，仅允许 {sorted(ALLOWED_EXT)}", file_hash, total
    if total == 0:
        return False, "文件为空", file_hash, total
    if total > MAX_SIZE:
        return False, f"文件超过大小上限 {MAX_SIZE // 1024 // 1024}MB", file_hash, total

    # 简易文件头魔数校验，防止改后缀蒙混。
    if ext == ".png" and head != b"\x89PNG":
        return False, "PNG 文件头校验失败", file_hash, total
    if ext in (".jpg", ".jpeg") and head[:3] != b"\xff\xd8\xff":
        return False, "JPG 文件头校验失败", file_hash, total
    return True, "ok", file_hash, total


def validate_file(file_bytes: bytes, filename: str):
    """兼容旧的 bytes 调用；主上传路径使用 inspect_file_stream。"""
    ok, msg, _, _ = inspect_file_stream(io.BytesIO(file_bytes), filename)
    return ok, msg
