"""对外 API 的数据结构。

设计原则：不同站点解析出的字段统一收敛到这里，站点原始字段不出现在响应中。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceInfo(BaseModel):
    """一个可用的漫画源。"""

    key: str = Field(description="源标识，用于 URL 中的 {source}")
    name: str = Field(description="展示名")
    needs_proxy: bool = Field(description="是否必须通过代理访问")
    prefer_proxy: bool = Field(default=False, description="是否「有代理就优先走代理」")
    image_hosts: list[str] = Field(default_factory=list, description="允许走图片中转的域名")


class ComicSummary(BaseModel):
    """搜索结果 / 列表中的漫画条目。"""

    id: str
    title: str
    cover: str | None = None
    authors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: str | None = None
    description: str | None = None


class ChapterInfo(BaseModel):
    """章节条目。"""

    id: str
    title: str
    group: str = Field(default="默认", description="章节分组名，例如「连载」")


class ComicDetail(BaseModel):
    """漫画详情（含章节列表）。"""

    id: str
    title: str
    cover: str | None = None
    description: str | None = None
    update_time: str | None = None
    tags: dict[str, list[str]] = Field(default_factory=dict)
    chapters: list[ChapterInfo] = Field(default_factory=list)


class SearchResult(BaseModel):
    """搜索响应。"""

    source: str
    keyword: str
    page: int
    count: int
    items: list[ComicSummary]


class ComicList(BaseModel):
    """不带关键词的列表响应（最近更新等）。"""

    source: str
    page: int
    count: int
    items: list[ComicSummary]


class ChapterImages(BaseModel):
    """章节图片直链。

    注意：多数站点的图片地址带签名与过期时间，客户端应及时消费。
    """

    source: str
    comic_id: str
    chapter_id: str
    title: str | None = None
    count: int
    images: list[str]
