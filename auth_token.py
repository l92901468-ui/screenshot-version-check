import hmac, hashlib, time, base64

# 演示用固定密钥；生产环境必须放环境变量/密钥管理服务，不能用硬编码
SECRET = b"change-me-in-prod"
TOKEN_TTL = 1800  # token 有效期(秒)，也就是提交时校验的 timestamp 窗口


def _sign(data: str) -> str:
    return hmac.new(SECRET, data.encode(), hashlib.sha256).hexdigest()


def issue_token(user_id: int) -> str:
    # payload = "user_id.exp"，exp 即过期时间戳，用于 timestamp 校验
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{user_id}.{exp}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str) -> int:
    # 返回 user_id；非法或过期抛 ValueError（对应 401/403 → 前端提示重新登录）
    try:
        payload, sig = token.rsplit(".", 1)
        user_id_s, exp_s = payload.split(".")
        user_id, exp = int(user_id_s), int(exp_s)
    except Exception:
        raise ValueError("token 格式非法")
    if not hmac.compare_digest(_sign(payload), sig):
        raise ValueError("token 签名错误（无权限）")
    if int(time.time()) > exp:
        raise ValueError("token 已过期（timestamp 不在有效期内）")
    return user_id
