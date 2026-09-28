"""httpx 客户端池。

按「是否需要代理」分成两个复用客户端：

* ``client(use_proxy=False)`` —— 直连，用于国内可直连的站点（如再漫画）。
* ``client(use_proxy=True)``  —— 走 ``JUJUBE_PROXY``，用于被墙/需要落地的站点（如拷贝漫画）。
"""

from __future__ import annotations

import httpx

from .config import Settings
from .errors import ProxyRequiredError


class HttpClientPool:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[bool, httpx.AsyncClient] = {}

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
                "headers": {
                    "User-Agent": self._settings.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            }
            if use_proxy:
                kwargs["proxy"] = self._settings.proxy
            client = httpx.AsyncClient(**kwargs)
            self._clients[use_proxy] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            if not client.is_closed:
                await client.aclose()
        self._clients.clear()
