import time, threading


class TTLCache:
    """进程内 TTL 缓存。

    - hard_ttl：硬过期，到点自动删除（下次访问时惰性清理 + 定时清理）
    - soft_ttl：软过期，命中但超过软 TTL 时需回源与 DB 核对版本，
                不一致则以 DB 为准（缓存只放即时用的东西，DB 才是权威）
    """

    def __init__(self, hard_ttl=10.0, soft_ttl=3.0):
        self.hard_ttl = hard_ttl
        self.soft_ttl = soft_ttl
        self._data = {}
        self._lock = threading.Lock()

    def _purge(self, now):
        expired = [k for k, (_, stored_at) in self._data.items()
                   if now - stored_at >= self.hard_ttl]
        for k in expired:
            del self._data[k]

    def get(self, key):
        """返回 (value, need_verify)。value=None 表示未命中；need_verify=True 表示需回源核对"""
        now = time.time()
        with self._lock:
            self._purge(now)
            item = self._data.get(key)
            if not item:
                return None, True
            value, stored_at = item
            return value, (now - stored_at) >= self.soft_ttl

    def set(self, key, value):
        with self._lock:
            self._data[key] = (value, time.time())

    def invalidate(self, key):
        with self._lock:
            self._data.pop(key, None)

    def size(self):
        with self._lock:
            self._purge(time.time())
            return len(self._data)
