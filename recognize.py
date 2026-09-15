"""识图后端选择 + 版本机械核验。

VISION_BACKEND=internal  -> 模拟内部大模型，不需要第三方 token / usage charge
VISION_BACKEND=external  -> 模拟第三方 API，经过 provider control + token 检查
"""

import os

REQUIRED_VERSION = "10.0.19045"


def parse_version(v: str):
    """把 "10.0.19045" 转成可比较的元组 (10, 0, 19045)。"""
    return tuple(int(p) for p in str(v).split(".") if p.isdigit())


def meets_version(detected: str, required: str = REQUIRED_VERSION) -> bool:
    return parse_version(detected) >= parse_version(required)


def backend_name() -> str:
    backend = os.environ.get("VISION_BACKEND", "internal").strip().lower()
    if backend not in {"internal", "external"}:
        raise ValueError("VISION_BACKEND must be 'internal' or 'external'")
    return backend


def uses_external_api() -> bool:
    return backend_name() == "external"


def call_internal_model(sid: int, retry_count: int):
    """内部识图模型模拟。

    id % 3 == 0        -> 服务异常，一直失败 -> 最终 DLQ
    id % 5 == 0 且首次 -> 首次超时，重试后成功
    id % 7 == 0        -> 识别出旧版本
    其余               -> 识别出符合要求的版本
    """
    if sid % 3 == 0:
        return False, None, "internal model unavailable"
    if sid % 5 == 0 and retry_count == 0:
        return False, None, "internal model timeout"
    version = "10.0.19041" if sid % 7 == 0 else "10.0.19045"
    return True, version, "internal model success"


def call_vision_model(sid: int, retry_count: int):
    if backend_name() == "external":
        # 延迟 import，避免 internal 模式无意义地初始化 provider control。
        import external_provider
        return external_provider.call_vision_model(sid, retry_count)
    return call_internal_model(sid, retry_count)
