import os, uuid, hashlib

UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def put_object(file_bytes: bytes, filename: str) -> dict:
    # ===== 这里就是“对象存储 (S3 / MinIO)”的接入点 =====
    # 演示用本地磁盘实现；接 MinIO/S3 时，把下面“写盘”换成
    #   client.put_object(Bucket=..., Key=key, Body=file_bytes)
    # 返回结构 (key/path/hash) 保持不变，上层无需改动。
    ext = os.path.splitext(filename)[1].lower()
    key = uuid.uuid4().hex + ext
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, key)
    with open(path, "wb") as f:
        f.write(file_bytes)
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    return {"key": key, "path": path, "hash": file_hash}
