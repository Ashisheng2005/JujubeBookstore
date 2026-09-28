"""对外 API 的数据结构。

设计原则：不同站点解析出的字段统一收敛到这里，站点原始字段不出现在响应中。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceInfo(BaseModel):
    """一个可用的源（漫画源或资源索引源）。"""

    key: str = Field(description="源标识，用于 URL 中的 {source}")
    name: str = Field(description="展示名")
    kind: str = Field(default="comic", description="源类型：comic=图片漫画源，resource=BT/磁力资源源")
    needs_proxy: bool = Field(description="是否必须通过代理访问")
    prefer_proxy: bool = Field(default=False, description="是否「有代理就优先走代理」")
    prefer_ipv4: bool = Field(default=False, description="无代理时是否强制走 IPv4（应对被污染的 AAAA 记录）")
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


# --- 资源索引源（BT / 磁力） ------------------------------------------------
class ResourceItem(BaseModel):
    """一条 BT 资源（列表项）。"""

    id: str = Field(description="资源 id；纯数字，或 /topics/view/... 路径")
    title: str
    category: str | None = None
    size: str | None = None
    published_at: str | None = None
    publisher: str | None = Field(default=None, description="发布组")
    uploader: str | None = Field(default=None, description="发布人")
    magnet: str | None = None
    torrent: str | None = Field(default=None, description=".torrent 下载地址（列表页通常没有）")
    seeders: str | None = None
    leechers: str | None = None
    completed: str | None = None
    comments: int | None = None
    detail_url: str | None = None


class ResourceFile(BaseModel):
    """种子内的文件条目。"""

    name: str
    size: str | None = None


class ResourceDetail(ResourceItem):
    """资源详情：磁力、种子、文件列表。"""

    info: dict[str, str] = Field(default_factory=dict, description="上游 info 行，如 所屬分類/發佈時間/文件大小")
    files: list[ResourceFile] = Field(default_factory=list)


class ResourceList(BaseModel):
    """资源列表响应。"""

    source: str
    page: int
    count: int
    items: list[ResourceItem]


class ResourceSearchResult(ResourceList):
    """资源搜索响应。"""

    keyword: str
    category: str | None = None
