"""漫画源注册表。"""

from __future__ import annotations

from ..http_client import HttpClientPool
from .base import ComicSource
from .mangabz import MangabzSource
from .zaimanhua import ZaimanhuaSource

#: 注册顺序即 ``/api/sources`` 的返回顺序
SOURCE_CLASSES: tuple[type[ComicSource], ...] = (ZaimanhuaSource, MangabzSource)

_REGISTRY: dict[str, type[ComicSource]] = {cls.key: cls for cls in SOURCE_CLASSES}


def register(source_cls: type[ComicSource]) -> type[ComicSource]:
    """注册新源（测试或运行时扩展用）。"""
    if not source_cls.key:
        raise ValueError("漫画源必须定义 key")
    _REGISTRY[source_cls.key] = source_cls
    return source_cls


def available_keys() -> list[str]:
    return list(_REGISTRY)


def create_source(key: str, pool: HttpClientPool) -> ComicSource:
    try:
        source_cls = _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(key) from exc
    return source_cls(pool)


__all__ = [
    "ComicSource",
    "SOURCE_CLASSES",
    "MangabzSource",
    "ZaimanhuaSource",
    "available_keys",
    "create_source",
    "register",
]
