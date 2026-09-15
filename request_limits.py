class RequestBodyTooLarge(Exception):
    """请求体超过应用允许的硬上限。"""


class LengthRequired(Exception):
    """上传请求缺少可验证的 Content-Length。"""


class InvalidContentLength(Exception):
    """Content-Length 非法。"""


def checked_content_length(headers, max_bytes: int) -> int:
    """在 multipart parser 读取 body 前检查请求长度。

    这个 demo 不直接接受 Transfer-Encoding: chunked；生产入口由 nginx / gateway
    先做解码和 body-size 限制，再向本 API 转发带 Content-Length 的请求。
    """
    transfer_encoding = (headers.get("Transfer-Encoding") or "").strip()
    if transfer_encoding:
        raise LengthRequired("上传接口要求 Content-Length，不直接接受 Transfer-Encoding")

    raw = headers.get("Content-Length")
    if raw is None:
        raise LengthRequired("上传接口要求 Content-Length")

    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidContentLength("Content-Length 非法")

    if value < 0:
        raise InvalidContentLength("Content-Length 不能为负数")
    if value > int(max_bytes):
        raise RequestBodyTooLarge(
            f"请求体 {value} bytes 超过上限 {int(max_bytes)} bytes"
        )
    return value


class LimitedReader:
    """给 multipart parser 加一层实际读取字节数的硬上限。

    Content-Length 是 fast-path；这个 wrapper 是 defense-in-depth。即使 parser 因 malformed
    multipart 继续读取，也最多只允许 max_bytes，再多一个字节就抛 RequestBodyTooLarge。
    """

    def __init__(self, raw, max_bytes: int):
        self.raw = raw
        self.max_bytes = int(max_bytes)
        if self.max_bytes < 0:
            raise ValueError("max_bytes 不能为负数")
        self.bytes_read = 0

    @property
    def remaining(self) -> int:
        return self.max_bytes - self.bytes_read

    def _check(self, data: bytes) -> bytes:
        if self.bytes_read + len(data) > self.max_bytes:
            self.bytes_read += len(data)
            raise RequestBodyTooLarge(
                f"实际读取请求体超过 {self.max_bytes} bytes"
            )
        self.bytes_read += len(data)
        return data

    def read(self, size=-1):
        # 最多向底层多探测 1 byte，用它判断是否真的越过硬上限。
        if size is None or size < 0:
            want = self.remaining + 1
        else:
            want = min(int(size), self.remaining + 1)
        return self._check(self.raw.read(want))

    def readline(self, size=-1):
        if size is None or size < 0:
            want = self.remaining + 1
        else:
            want = min(int(size), self.remaining + 1)
        return self._check(self.raw.readline(want))

    def __getattr__(self, name):
        return getattr(self.raw, name)
