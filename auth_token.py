import hashlib
import hmac
import time

import db

# 演示用固定密钥；生产环境必须放环境变量/密钥管理服务，不能用硬编码
SECRET = b"change-me-in-prod"
TOKEN_TTL = 1800  # token 有效期(秒)


def _sign(data: str) -> str:
    return hmac.new(SECRET, data.encode(), hashlib.sha256).hexdigest()


def issue_token(user_id: int) -> str:
    """签发带 token_version 的短期 access token。

    token 仍然是自包含签名数据，但 version 来自共享认证状态。账号如果恰好在
    登录校验与签发之间被 disable，这里仍可能签出一个 token；不过下一次请求会
    因共享 is_active/token_version 检查立刻失败，不会获得访问权限。
    """
    state = db.get_user_auth_state(user_id)
    if state is None:
        raise ValueError("账号不存在")
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{user_id}.{exp}.{int(state['token_version'])}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str) -> int:
    """校验签名/过期时间，并核对共享账号状态以支持立即撤销。"""
    try:
        payload, sig = token.rsplit(".", 1)
        user_id_s, exp_s, version_s = payload.split(".")
        user_id = int(user_id_s)
        exp = int(exp_s)
        token_version = int(version_s)
    except Exception:
        # 旧的 user_id.exp.signature token 也会落到这里，因此部署该版本会要求重新登录。
        raise ValueError("token 格式非法或版本过旧，请重新登录")

    if not hmac.compare_digest(_sign(payload), sig):
        raise ValueError("token 签名错误（无权限）")
    if int(time.time()) > exp:
        raise ValueError("token 已过期（timestamp 不在有效期内）")

    # 不使用 API 实例本地缓存：否则 cache TTL 会重新引入“禁用后仍可访问一段时间”的窗口。
    state = db.get_user_auth_state(user_id)
    if state is None:
        raise ValueError("账号不存在")
    if not state["is_active"]:
        raise ValueError("账号已禁用")
    if int(state["token_version"]) != token_version:
        raise ValueError("token 已被撤销，请重新登录")

    return user_id
