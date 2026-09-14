import os

MAX_SIZE = 10 * 1024 * 1024  # 10MB 上限
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def validate_file(file_bytes: bytes, filename: str):
    # 返回 (ok, msg)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return False, f"文件格式不支持，仅允许 {sorted(ALLOWED_EXT)}"
    if len(file_bytes) == 0:
        return False, "文件为空"
    if len(file_bytes) > MAX_SIZE:
        return False, f"文件超过大小上限 {MAX_SIZE // 1024 // 1024}MB"
    # 简易文件头魔数校验，防止改后缀蒙混
    head = file_bytes[:4]
    if ext == ".png" and head != b"\x89PNG":
        return False, "PNG 文件头校验失败"
    if ext in (".jpg", ".jpeg") and head[:3] != b"\xff\xd8\xff":
        return False, "JPG 文件头校验失败"
    return True, "ok"
