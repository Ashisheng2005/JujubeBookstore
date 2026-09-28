"""运行配置。

所有配置项都可用环境变量覆盖，前缀 ``JUJUBE_``，例如::

    JUJUBE_PROXY=http://127.0.0.1:7897
    JUJUBE_TIMEOUT=20

未显式设置 ``JUJUBE_PROXY`` 时会回退到标准的 ``HTTPS_PROXY`` / ``HTTP_PROXY``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PREFIX = "JUJUBE_"

#: 默认 UA。部分国漫站对桌面浏览器 UA 会返回不同页面结构，统一使用移动端 UA。
DEFAULT_USER_AGENT = "Mozilla/5.0 (Linux; Android 12) Mobile"


def _raw(name: str) -> str | None:
    value = os.environ.get(PREFIX + name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_str(name: str, default: str) -> str:
    return _raw(name) or default


def _env_opt_str(name: str) -> str | None:
    return _raw(name)


def _env_bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = _raw(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = _raw(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """服务配置。"""

    host: str
    port: int
    proxy: str | None
    timeout: float
    user_agent: str
    detail_ttl: int
    chapter_ttl: int
    image_proxy_enabled: bool
    image_proxy_max_bytes: int

    @property
    def proxy_enabled(self) -> bool:
        return bool(self.proxy)


def load_settings() -> Settings:
    """从环境变量构造配置对象。"""
    proxy = _env_opt_str("PROXY")
    if proxy is None:
        # 回退到宿主环境里常见的标准变量
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip() or None
    return Settings(
        host=_env_str("HOST", "127.0.0.1"),
        port=_env_int("PORT", 8000),
        proxy=proxy,
        timeout=_env_float("TIMEOUT", 20.0),
        user_agent=_env_str("USER_AGENT", DEFAULT_USER_AGENT),
        detail_ttl=_env_int("DETAIL_TTL", 600),
        chapter_ttl=_env_int("CHAPTER_TTL", 300),
        image_proxy_enabled=_env_bool("IMAGE_PROXY", True),
        image_proxy_max_bytes=_env_int("IMAGE_PROXY_MAX_BYTES", 15 * 1024 * 1024),
    )
