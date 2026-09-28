"""源共用的传输层：客户端选择与代理策略。

漫画源（``ComicSource``）与资源索引源（``ResourceSource``）都继承这里，
避免两套抽象各写一遍代理判断。
"""

from __future__ import annotations

from .http_client import HttpClientPool


class SourceTransport:
    #: URL 里使用的源标识，全局唯一
    key: str = ""
    #: 展示名
    name: str = ""
    #: 是否必须走代理（未配置代理时直接报 503）
    needs_proxy: bool = False
    #: 是否「有代理就优先走代理」：直连可用但不稳时用
    prefer_proxy: bool = False
    #: 没有代理时是否强制走 IPv4：站点 AAAA 记录是伪地址、httpcore 会卡在 IPv6 超时的情况
    prefer_ipv4: bool = False

    def __init__(self, pool: HttpClientPool) -> None:
        self.pool = pool

    @property
    def client(self):
        if self.needs_proxy:
            return self.pool.client(use_proxy=True)
        if self.prefer_proxy and getattr(self.pool, "proxy_available", False):
            return self.pool.client(use_proxy=True)
        if self.prefer_ipv4:
            return self.pool.client(use_proxy=False, local_address="0.0.0.0")
        return self.pool.client(use_proxy=False)

    def base_info(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "needs_proxy": self.needs_proxy,
            "prefer_proxy": self.prefer_proxy,
            "prefer_ipv4": self.prefer_ipv4,
        }
