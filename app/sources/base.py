"""漫画源抽象。

新增一个站点只需要：

1. 继承 :class:`ComicSource`，实现 ``search`` / ``detail`` / ``chapter``；
2. 在 ``app/sources/__init__.py`` 里注册。

解析层只负责把站点数据转成 ``schemas`` 里的统一结构，不关心 HTTP 层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..http_client import HttpClientPool
from ..schemas import ChapterImages, ComicDetail, ComicSummary


class ComicSource(ABC):
    #: URL 里使用的源标识，全局唯一
    key: str = ""
    #: 展示名
    name: str = ""
    #: 是否必须走代理
    needs_proxy: bool = False
    #: 允许走图片中转的域名（用于防盗链图片代理，防止 SSRF）
    image_hosts: tuple[str, ...] = ()
    #: 请求图片时使用的 Referer
    image_referer: str | None = None

    def __init__(self, pool: HttpClientPool) -> None:
        self.pool = pool

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
    @property
    def client(self):
        return self.pool.client(use_proxy=self.needs_proxy)

    def info(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "needs_proxy": self.needs_proxy,
            "image_hosts": list(self.image_hosts),
        }
