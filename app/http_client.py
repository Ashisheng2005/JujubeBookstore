"""httpx 客户端池。

按「是否需要代理」分成两个复用客户端：

* ``client(use_proxy=False)`` —— 直连，用于国内可直连的站点（如再漫画）。
* ``client(use_proxy=True)``  —— 走 ``JUJUBE_PROXY``，用于被墙/需要落地的站点（如拷贝漫画）。

两个客户端都带**幂等 GET 重试**：漫画站点的连接超时/对端断连非常常见（实测 Mangabz
偶发 ``ConnectTimeout``），而搜索/详情/章节都是幂等请求，重试代价极低。
"""

from __future__ import annotations

import asyncio
import ssl
from functools import lru_cache

import certifi
import httpx

from .config import Settings
from .errors import ProxyRequiredError

#: 值得重试的传输层异常（都是「没拿到响应」，重试安全）
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


@lru_cache(maxsize=1)
def default_ssl_context() -> ssl.SSLContext:
    """进程级复用 SSL 上下文。

    在 Windows 上 ``create_default_context(certifi.where())`` 单次要 1s+，
    每个客户端构造一遍会把服务启动和测试都拖慢。
    """
    return ssl.create_default_context(cafile=certifi.where())


class RetryingClient(httpx.AsyncClient):
    """只对 GET 做重试，POST/DELETE 不碰（避免非幂等副作用）。"""

    def __init__(self, *, retries: int = 2, backoff: float = 0.4, **kwargs) -> None:
        super().__init__(**kwargs)
        self.retries = retries
        self.backoff = backoff

    async def get(self, url, **kwargs) -> httpx.Response:  # type: ignore[override]
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return await super().get(url, **kwargs)
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                await asyncio.sleep(self.backoff * (attempt + 1))
        assert last_error is not None
        raise last_error


class HttpClientPool:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[bool, httpx.AsyncClient] = {}

    @property
    def proxy_available(self) -> bool:
        """是否配置了代理（供 ``prefer_proxy`` 的源判断）。"""
        return self._settings.proxy_enabled

    def client(self, *, use_proxy: bool) -> httpx.AsyncClient:
        if use_proxy and not self._settings.proxy_enabled:
            raise ProxyRequiredError(
                "该源需要通过代理访问，请设置环境变量 JUJUBE_PROXY（例如 http://127.0.0.1:7897）"
            )
        client = self._clients.get(use_proxy)
        if client is None or client.is_closed:
            kwargs: dict[str, object] = {
                "timeout": httpx.Timeout(self._settings.timeout),
                "follow_redirects": True,
                "retries": self._settings.http_retries,
                # 代理由 JUJUBE_PROXY 显式管理：关掉 trust_env 既避免误用系统代理，
                # 也避免 httpx 构造时去读 Windows 注册表
                "trust_env": False,
                "verify": default_ssl_context(),
                "headers": {
                    "User-Agent": self._settings.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            }
            if use_proxy:
                kwargs["proxy"] = self._settings.proxy
            client = RetryingClient(**kwargs)  # type: ignore[arg-type]
            self._clients[use_proxy] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            if not client.is_closed:
                await client.aclose()
        self._clients.clear()
