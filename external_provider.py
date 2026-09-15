"""外部识图 API 的最小模拟。

不发真实网络请求；重点是把与“内部模型”不同的工程边界显式出来：
- 需要 API token；
- provider 可以被全局暂停；
- token incident 会让旧 credential generation 失效；
- 外部依赖有 timeout / 5xx / 401 等失败模式。

真实生产里 token 应来自 secret manager，并通过 HTTPS/mTLS 等受控通道调用供应商。
"""

import os

import provider_control


def call_vision_model(sid: int, retry_count: int):
    state = provider_control.get_state()
    if not state or not state["enabled"] or state["token_status"] != "active":
        return False, None, "external provider paused: credential revoked/incident containment"

    token = os.environ.get("EXTERNAL_API_TOKEN")
    if not token:
        return False, None, "external provider token missing"

    # 模拟 worker 当前装载的 secret generation。incident rotate 后，旧 worker 即使还持有旧 token，
    # 也会因为 generation 不匹配而得到 401；真正系统应由 provider/secret manager 实际 enforce。
    configured_generation = int(os.environ.get("EXTERNAL_API_CREDENTIAL_VERSION", "1"))
    if configured_generation != int(state["credential_version"]):
        return False, None, "external provider 401: stale credential generation"

    # 确定性失败模式，方便测试 retry / DLQ，不需要随机数。
    if sid % 11 == 0:
        return False, None, "external provider 503"
    if sid % 5 == 0 and retry_count == 0:
        return False, None, "external provider timeout"

    version = "10.0.19041" if sid % 7 == 0 else "10.0.19045"
    return True, version, "external provider success"
