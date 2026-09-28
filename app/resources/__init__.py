"""资源索引源注册表（与漫画源注册表相互独立）。"""

from __future__ import annotations

from ..http_client import HttpClientPool
from .base import ResourceSource
from .dmhy import DmhySource

#: 注册顺序即 ``/api/resources`` 的返回顺序
RESOURCE_CLASSES: tuple[type[ResourceSource], ...] = (DmhySource,)

_REGISTRY: dict[str, type[ResourceSource]] = {cls.key: cls for cls in RESOURCE_CLASSES}


def register(source_cls: type[ResourceSource]) -> type[ResourceSource]:
    if not source_cls.key:
        raise ValueError("资源源必须定义 key")
    _REGISTRY[source_cls.key] = source_cls
    return source_cls


def available_keys() -> list[str]:
    return list(_REGISTRY)


def create_resource(key: str, pool: HttpClientPool) -> ResourceSource:
    try:
        source_cls = _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(key) from exc
    return source_cls(pool)


__all__ = [
    "DmhySource",
    "RESOURCE_CLASSES",
    "ResourceSource",
    "available_keys",
    "create_resource",
    "register",
]
