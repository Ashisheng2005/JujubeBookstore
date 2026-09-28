"""再漫画（zaimanhua）源。

接口来自 App 端 v4 API：``https://v4api.zaimanhua.com/app/v1``，匿名可用、无需签名，
国内可直连。三个核心链路均已实测：

* 搜索   ``search/index?source=0&keyword=&page=&size=20``   -> ``data.list[]``
* 详情   ``comic/detail/{id}?_v=2.2.5``                     -> ``data.data``
* 章节   ``comic/chapter/{comic_id}/{chapter_id}?_v=2.2.5`` -> ``data.data.page_url_hd``
* 更新   ``comic/update/list/0/{page}``                     -> ``data[]``

两个实测踩坑点（改版时优先怀疑这里）：

1. 详情**必须**用 ``?_v=2.2.5``，用 ``?channel=android`` 会对大量漫画返回
   ``errno=2 漫画不存在或已被删除``（其实是 android 渠道的版权/地区过滤）。
2. 搜索结果的 ``comic_id`` 恒为 0、更新列表的 ``id`` 恒为 0，取非 0 的那个才有效。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from ..errors import NotFoundError, SourceError
from ..schemas import ChapterImages, ChapterInfo, ComicDetail, ComicSummary
from .base import ComicSource

#: 搜索/更新列表里 ``id`` 与 ``comic_id`` 只有一个有值，另一个为 0，取非 0 的那个
_ID_ZERO = {"", "0", 0, None}

#: 「1」「12.5话」「连载3卷」这类标题统一成「第N话」
_CHAPTER_TITLE_RE = re.compile(r"^(?:连载版?)?(\d+\.?\d*)([话卷])?$")


def resolve_comic_id(item: dict[str, Any]) -> str:
    """从列表条目里解析出可用于详情接口的漫画 id。"""
    for field in ("comic_id", "id"):
        value = item.get(field)
        if value not in _ID_ZERO:
            return str(value)
    return ""


def normalize_chapter_title(raw: Any) -> str:
    title = "" if raw is None else str(raw).strip()
    if not title:
        return title
    match = _CHAPTER_TITLE_RE.match(title)
    if match:
        return f"第{match.group(1)}{match.group(2) or '话'}"
    return title


def split_tags(value: Any) -> list[str]:
    """``types`` 在列表接口里是逗号串，在详情接口里是对象数组。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace("/", ",").split(",") if part.strip()]
    if isinstance(value, list):
        return [str(tag.get("tag_name", "")).strip() for tag in value if isinstance(tag, dict) and tag.get("tag_name")]
    return []


class ZaimanhuaSource(ComicSource):
    key = "zaimanhua"
    name = "再漫画"
    needs_proxy = False
    image_hosts = ("images.zaimanhua.com",)
    image_referer = "https://www.zaimanhua.com/"

    base_url = "https://v4api.zaimanhua.com/app/v1"
    #: App 端接口版本号，缺失会被上游按旧版渠道处理（见模块 docstring）
    api_version = "2.2.5"

    # --- 底层请求 -----------------------------------------------------
    async def _get_json(
        self, path: str, *, headers: dict[str, str] | None = None, **params: Any
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = await self.client.get(url, params=params, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SourceError(f"再漫画返回 HTTP {exc.response.status_code}", source=self.key) from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"请求再漫画失败: {exc}", source=self.key) from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SourceError("再漫画返回内容不是合法 JSON", source=self.key) from exc

        errno = payload.get("errno")
        if errno not in (0, None):
            message = payload.get("errmsg") or f"errno={errno}"
            if errno == 2:
                raise NotFoundError(str(message), source=self.key)
            raise SourceError(f"再漫画业务错误: {message}", source=self.key)
        return payload

    @staticmethod
    def _data(payload: dict[str, Any]) -> Any:
        return payload.get("data")

    # --- 解析 ---------------------------------------------------------
    def _summary(self, item: dict[str, Any]) -> ComicSummary:
        comic_id = resolve_comic_id(item)
        status = item.get("status") or None
        tags = split_tags(item.get("types"))
        if status:
            tags = [str(status), *[t for t in tags if t != status]]
        description = item.get("description") or item.get("last_update_chapter_name") or item.get("last_name")
        return ComicSummary(
            id=comic_id,
            title=str(item.get("title") or item.get("name") or ""),
            cover=item.get("cover"),
            authors=split_tags(item.get("authors")),
            tags=tags,
            status=str(status) if status else None,
            description=str(description) if description else None,
        )

    @staticmethod
    def _format_time(ts: Any) -> str | None:
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    # --- 能力实现 -----------------------------------------------------
    async def search(self, keyword: str, page: int = 1) -> list[ComicSummary]:
        payload = await self._get_json(
            "search/index", source=0, keyword=keyword, page=page, sort=0, size=20
        )
        data = self._data(payload) or {}
        items = data.get("list") or []
        return [self._summary(item) for item in items]

    async def latest(self, page: int = 1) -> list[ComicSummary]:
        """最近更新列表（首页/探索用）。"""
        payload = await self._get_json(f"comic/update/list/0/{page}")
        items = self._data(payload) or []
        return [self._summary(item) for item in items]

    async def detail(self, comic_id: str) -> ComicDetail:
        payload = await self._get_json(
            f"comic/detail/{comic_id}", _v=self.api_version, headers={"Platform": "pc"}
        )
        data = (self._data(payload) or {}).get("data") or {}
        if not data:
            raise NotFoundError(f"再漫画未找到漫画 {comic_id}", source=self.key)

        chapters: list[ChapterInfo] = []
        for group in data.get("chapters") or []:
            group_title = str(group.get("title") or "默认")
            # 上游按「最新在前」返回，倒序后变成从第 1 话开始的阅读顺序
            for chapter in reversed(group.get("data") or []):
                chapter_id = chapter.get("chapter_id")
                if chapter_id in _ID_ZERO:
                    continue
                chapters.append(
                    ChapterInfo(
                        id=str(chapter_id),
                        title=normalize_chapter_title(chapter.get("chapter_title")),
                        group=group_title,
                    )
                )

        tags = {
            "作者": split_tags(data.get("authors")),
            "状态": split_tags(data.get("status")),
            "标签": split_tags(data.get("types")),
        }
        last_chapter = data.get("last_update_chapter_name")
        if last_chapter:
            tags["状态"] = [*tags["状态"], str(last_chapter)]

        return ComicDetail(
            id=str(data.get("id") or comic_id),
            title=str(data.get("title") or ""),
            cover=data.get("cover"),
            description=data.get("description") or None,
            update_time=self._format_time(data.get("last_updatetime")),
            tags={key: value for key, value in tags.items() if value},
            chapters=chapters,
        )

    async def chapter(self, comic_id: str, chapter_id: str) -> ChapterImages:
        payload = await self._get_json(
            f"comic/chapter/{comic_id}/{chapter_id}",
            _v=self.api_version,
            headers={"Platform": "h5"},
        )
        data = (self._data(payload) or {}).get("data") or {}
        if data.get("canRead") is False:
            raise SourceError(f"章节 {chapter_id} 需要登录后阅读（canRead=false）", source=self.key)
        images = data.get("page_url_hd") or data.get("page_url") or []
        if not images:
            raise NotFoundError(f"再漫画章节 {chapter_id} 没有可用图片", source=self.key)
        return ChapterImages(
            source=self.key,
            comic_id=str(comic_id),
            chapter_id=str(chapter_id),
            title=str(data.get("title")) if data.get("title") else None,
            count=len(images),
            images=[str(url) for url in images],
        )
