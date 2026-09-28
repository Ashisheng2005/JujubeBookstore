"""漫画源抽象（图片阅读型）。

新增一个站点只需要：

1. 继承 :class:`ComicSource`，实现 ``search`` / ``detail`` / ``chapter``；
2. 在 ``app/sources/__init__.py`` 里注册。

解析层只负责把站点数据转成 ``schemas`` 里的统一结构，不关心 HTTP 层。
BT/磁力这类「资源索引」不是图片源，走 ``app/resources`` 里的 ``ResourceSource``。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ..schemas import ChapterImages, ComicDetail, ComicSummary
from ..transport import SourceTransport


class ComicSource(SourceTransport, ABC):
    #: 允许走图片中转的域名（用于防盗链图片代理，防止 SSRF）
    image_hosts: tuple[str, ...] = ()
    #: 允许走图片中转的域名正则（用于「泛域名 CDN」，例如拷贝漫画的 *.mangafunb.fun）
    image_host_patterns: tuple[str, ...] = ()
    #: 请求图片时使用的 Referer
    image_referer: str | None = None

    # --- 三个核心能力 -------------------------------------------------
    @abstractmethod
    async def search(self, keyword: str, page: int = 1) -> list[ComicSummary]:
        """按关键词搜索漫画。"""

    @abstractmethod
    async def detail(self, comic_id: str) -> ComicDetail:
        """获取漫画详情与章节列表。"""

    @abstractmethod
    async def chapter(self, comic_id: str, chapter_id: str) -> ChapterImages:
        """获取某章节的图片直链。"""

    # --- 辅助 ---------------------------------------------------------
    def allows_image_host(self, host: str) -> bool:
        """图片中转的域名白名单校验（后缀匹配 + 正则匹配）。"""
        host = host.lower()
        if any(host == allowed or host.endswith("." + allowed) for allowed in self.image_hosts):
            return True
        return any(re.fullmatch(pattern, host) for pattern in self.image_host_patterns)

    def info(self) -> dict[str, object]:
        return {
            **self.base_info(),
            "kind": "comic",
            "image_hosts": [*self.image_hosts, *self.image_host_patterns],
        }
