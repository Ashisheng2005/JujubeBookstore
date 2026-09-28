"""极简 TTL 缓存。

用于缓存详情与章节解析结果；章节图片直链带签名与过期时间，所以 TTL 必须短。
单进程 asyncio 场景下 dict 读写已足够，无需加锁。
"""

from __future__ import annotations

import time
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, ttl: float, maxsize: int = 512) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._data: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        item = self._data.get(key)
        if item is None:
            return None
        expire_at, value = item
        if expire_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: T) -> None:
        if len(self._data) >= self._maxsize:
            self._evict()
        self._data[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()

    def _evict(self) -> None:
        now = time.monotonic()
        for key in [k for k, (expire_at, _) in self._data.items() if expire_at < now]:
            self._data.pop(key, None)
        # 仍然超限则丢弃最早写入的一批（dict 保持插入顺序）
        overflow = len(self._data) - self._maxsize // 2
        for key in list(self._data)[: max(overflow, 0)]:
            self._data.pop(key, None)
