"""HTTP 层测试：代理选择策略与幂等 GET 重试。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.errors import ProxyRequiredError
from app.http_client import HttpClientPool, RetryingClient, default_ssl_context
from app.sources.base import ComicSource


class StubPool:
    """只记录 client 选择结果的假客户端池。"""

    def __init__(self, *, proxy_available: bool) -> None:
        self.proxy_available = proxy_available
        self.requested: list[bool] = []

    def client(self, *, use_proxy: bool):
        self.requested.append(use_proxy)
        return object()


class DirectSource(ComicSource):
    key = "direct"
    name = "直连源"

    async def search(self, keyword, page=1): ...
    async def detail(self, comic_id): ...
    async def chapter(self, comic_id, chapter_id): ...


class NeedsProxySource(DirectSource):
    key = "needs"
    needs_proxy = True


class PreferProxySource(DirectSource):
    key = "prefer"
    prefer_proxy = True


def test_direct_source_never_uses_proxy():
    pool = StubPool(proxy_available=True)
    DirectSource(pool).client  # noqa: B018 - 只取属性
    assert pool.requested == [False]


def test_needs_proxy_source_always_uses_proxy():
    pool = StubPool(proxy_available=True)
    NeedsProxySource(pool).client  # noqa: B018
    assert pool.requested == [True]


def test_prefer_proxy_uses_proxy_only_when_configured():
    with_proxy = StubPool(proxy_available=True)
    PreferProxySource(with_proxy).client  # noqa: B018
    assert with_proxy.requested == [True]

    without_proxy = StubPool(proxy_available=False)
    PreferProxySource(without_proxy).client  # noqa: B018
    assert without_proxy.requested == [False]


def test_pool_requires_proxy_for_needs_proxy_sources():
    settings = Settings(
        host="127.0.0.1",
        port=8000,
        proxy=None,
        timeout=5,
        user_agent="test",
        detail_ttl=1,
        chapter_ttl=1,
        image_proxy_enabled=True,
        image_proxy_max_bytes=1024,
        http_retries=1,
    )
    pool = HttpClientPool(settings)
    with pytest.raises(ProxyRequiredError):
        pool.client(use_proxy=True)
    assert pool.proxy_available is False


def test_retrying_client_retries_transient_connect_timeout():
    """第一次连接超时，第二次成功 —— 应该返回结果而不是抛错。"""
    attempts = {"n": 0}
    # 复用同一份 SSL 上下文，避免每个测试重复加载证书（Windows 上单次 1~4s）
    client = RetryingClient(retries=2, backoff=0.01, verify=default_ssl_context())
    real_get = httpx.AsyncClient.get

    async def flaky_get(self, url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectTimeout("boom")
        return httpx.Response(200, text="ok")

    httpx.AsyncClient.get = flaky_get  # type: ignore[method-assign]
    try:
        response = asyncio.run(client.get("https://example.com"))
    finally:
        httpx.AsyncClient.get = real_get  # type: ignore[method-assign]
        asyncio.run(client.aclose())

    assert response.status_code == 200
    assert attempts["n"] == 2


def test_retrying_client_gives_up_after_retries():
    attempts = {"n": 0}
    client = RetryingClient(retries=1, backoff=0.01, verify=default_ssl_context())
    real_get = httpx.AsyncClient.get

    async def always_fail(self, url, **kwargs):
        attempts["n"] += 1
        raise httpx.ReadTimeout("boom")

    httpx.AsyncClient.get = always_fail  # type: ignore[method-assign]
    try:
        with pytest.raises(httpx.ReadTimeout):
            asyncio.run(client.get("https://example.com"))
    finally:
        httpx.AsyncClient.get = real_get  # type: ignore[method-assign]
        asyncio.run(client.aclose())

    assert attempts["n"] == 2  # 首次 + 1 次重试
