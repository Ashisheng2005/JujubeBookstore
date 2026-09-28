"""资源索引源抽象（BT / 磁力）。

和漫画源的区别：这里交付的是 **magnet / .torrent**，没有「章节」和「图片直链」，
所以单独一套模型与路由（``/api/resources/...``），不塞进 ``ComicSource``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..schemas import ResourceDetail, ResourceItem
from ..transport import SourceTransport


class ResourceSource(SourceTransport, ABC):
    """BT / 磁力资源索引源。"""

    @abstractmethod
    async def search(
        self, keyword: str, page: int = 1, category: str | None = None
    ) -> list[ResourceItem]:
        """按关键词搜索资源。"""

    @abstractmethod
    async def detail(self, item_id: str) -> ResourceDetail:
        """获取单条资源详情（磁力、种子、文件列表）。"""

    async def latest(self, page: int = 1, category: str | None = None) -> list[ResourceItem]:
        """最新发布列表。默认不支持，子类可覆盖。"""
        raise NotImplementedError(f"源 {self.key!r} 未实现最新发布列表")

    def info(self) -> dict[str, object]:
        return {**self.base_info(), "kind": "resource"}
