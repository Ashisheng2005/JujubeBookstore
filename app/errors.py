"""站点解析层的异常，由 API 层统一翻译成 HTTP 状态码。"""

from __future__ import annotations


class SourceError(RuntimeError):
    """源解析失败（网络、结构变更、上游业务错误等）。"""

    def __init__(self, message: str, *, source: str | None = None) -> None:
        super().__init__(message)
        self.source = source


class NotFoundError(SourceError):
    """上游明确表示资源不存在。"""


class ProxyRequiredError(SourceError):
    """该源需要代理但当前没有配置。"""
