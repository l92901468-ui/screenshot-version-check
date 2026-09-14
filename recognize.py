# 识图模型调用 + 版本机械核验（模型调用是模拟的，没有真 API）

REQUIRED_VERSION = "10.0.19045"   # 机械核验的门槛版本：识别出的版本 >= 它才算通过


def parse_version(v: str):
    """把 "10.0.19045" 转成可比较的元组 (10, 0, 19045)"""
    return tuple(int(p) for p in str(v).split(".") if p.isdigit())


def meets_version(detected: str, required: str = REQUIRED_VERSION) -> bool:
    """机械核验：识别版本是否达到门槛版本"""
    return parse_version(detected) >= parse_version(required)


def call_vision_model(sid: int, retry_count: int):
    """调用识图模型，从截图里识别出电脑版本号。

    ===== 模拟点（真实场景把这里换成真的识图模型 API 调用）=====
    返回 (ok, version, msg)：ok=False 表示识别失败（可重试）。
    演示用确定性规则：
      id % 3 == 0        → 模型服务异常，一直失败 → 最终进 DLQ
      id % 5 == 0 且首次 → 首次调用超时，重试后成功
      id % 7 == 0        → 识别出旧版本（核验未通过，但流程算完成）
      其余                → 识别出符合要求的版本（核验通过）
    """
    if sid % 3 == 0:
        return False, None, "模拟：识图模型服务异常"
    if sid % 5 == 0 and retry_count == 0:
        return False, None, "模拟：识图模型调用超时"
    version = "10.0.19041" if sid % 7 == 0 else "10.0.19045"
    return True, version, "模拟：识别成功"
