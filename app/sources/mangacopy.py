"""拷贝漫画（CopyManga）源。

**必须通过代理访问**（``needs_proxy = True``，走 ``JUJUBE_PROXY``）。接口来自官方 App v3，
参数以 App 逆向文档为准（fumiama/copymanga），实测踩坑点：

1. **域名**：``api.mangacopy.com`` 已对第三方返回 ``code=210 請升級到最新的APP(3.0.0)``，
   能用的是 ``api.2024manga.com``（索引最全，搜索命中数是前者的 9 倍）与 ``www.mangacopy.com``，
   所以这里做了节点轮换。
2. **``platform`` 请求头是开关**：缺了它搜索恒返回 ``total=0``（不是被墙，也不是关键词问题）。
3. **详情与章节是两套接口**：``comic2/{path_word}`` 里的 ``groups`` 常常内嵌 0 章节，
   真正的章节列表要再打 ``comic/{pw}/group/{group}/chapters``，且每页上限 100、需要翻页。
4. **图片端点不是文档里的 ``chapter2/{uuid}``**：在 ``api.2024manga.com`` 上 ``chapter2`` 是 404，
   要用 ``comic/{pw}/chapter/{uuid}``。
5. **最近更新**：文档的 ``update/newest`` 已 404，改用 ``comics?ordering=-datetime_updated``。
6. 图片 CDN 是泛域名（``sh.mangafunb.fun`` 之类），所以白名单用正则而不是固定域名。
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import NotFoundError, SourceError
from ..schemas import ChapterImages, ChapterInfo, ComicDetail, ComicSummary
from .base import ComicSource

#: 每页条数（App 默认 21）
PAGE_SIZE = 21
#: 章节列表接口每页上限
CHAPTER_PAGE_SIZE = 100
#: 上游「换节点」提示码：210 = 需要升级 App / 位置信息不能为空
_NODE_ERROR_CODES = {210}


class MangacopySource(ComicSource):
    key = "mangacopy"
    name = "拷贝漫画"
    needs_proxy = True
    image_hosts = ("mangacopy.com",)
    #: 图片 CDN 是泛域名（如 sh.mangafunb.fun），用正则白名单
    image_host_patterns = (r"[0-9a-z-]+\.mangafun[a-z]\.(?:xyz|fun)",)
    image_referer = "https://www.mangacopy.com/"

    #: 按顺序尝试，遇节点错误自动切换
    hosts = ("https://api.2024manga.com", "https://www.mangacopy.com")

    api_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36 Edg/141.0.0.0"
        ),
        "Accept": "application/json",
        "platform": "3",
        "version": "2024.4.28",
        "webp": "1",
        "x-requested-with": "com.manga2020.app",
    }

    # --- 底层请求（带节点轮换） ---------------------------------------
    async def _get_results(self, path: str, **params: Any) -> dict[str, Any]:
        params.setdefault("platform", 3)
        last_error: SourceError | None = None
        for host in self.hosts:
            url = f"{host}/api/v3/{path.lstrip('/')}"
            try:
                resp = await self.client.get(url, params=params, headers=self.api_headers)
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                last_error = SourceError(f"请求拷贝漫画失败: {exc}", source=self.key)
                continue
            except ValueError as exc:
                last_error = SourceError("拷贝漫画返回内容不是合法 JSON", source=self.key)
                continue

            code = payload.get("code")
            if code == 200:
                return payload.get("results") or {}
            message = str(payload.get("message") or payload.get("detail") or f"code={code}")
            if code in _NODE_ERROR_CODES:
                last_error = SourceError(f"拷贝漫画节点不可用: {message}", source=self.key)
                continue
            raise SourceError(f"拷贝漫画业务错误: {message}", source=self.key)
        raise last_error or SourceError("拷贝漫画所有节点均不可用", source=self.key)

    # --- 解析 ---------------------------------------------------------
    @staticmethod
    def _names(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("display")
            else:
                name = item
            if name:
                names.append(str(name))
        return names

    @staticmethod
    def _display(value: Any) -> str | None:
        if isinstance(value, dict):
            display = value.get("display") or value.get("name")
            return str(display) if display else None
        if isinstance(value, list):
            names = MangacopySource._names(value)
            return names[0] if names else None
        return str(value) if value else None

    def _summary(self, item: dict[str, Any]) -> ComicSummary:
        status = self._display(item.get("status"))
        tags = self._names(item.get("theme"))
        if status:
            tags.append(status)
        return ComicSummary(
            id=str(item.get("path_word") or ""),
            title=str(item.get("name") or ""),
            cover=item.get("cover"),
            authors=self._names(item.get("author")),
            tags=tags,
            status=status,
            description=(item.get("alias") or None),
        )

    # --- 能力实现 -----------------------------------------------------
    async def search(self, keyword: str, page: int = 1) -> list[ComicSummary]:
        results = await self._get_results(
            "search/comic",
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
            q=keyword,
            q_type="",
        )
        return [self._summary(item) for item in results.get("list") or []]

    async def latest(self, page: int = 1) -> list[ComicSummary]:
        """最近更新。

        文档里的 ``update/newest`` 实测在两个域名上都是 404，改用目录接口按更新时间倒序。
        """
        results = await self._get_results(
            "comics",
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
            ordering="-datetime_updated",
        )
        return [self._summary(item) for item in results.get("list") or []]

    async def _group_chapters(self, path_word: str, group: str) -> list[ChapterInfo]:
        collected: list[tuple[float, ChapterInfo]] = []
        offset = 0
        while True:
            results = await self._get_results(
                f"comic/{path_word}/group/{group}/chapters",
                limit=CHAPTER_PAGE_SIZE,
                offset=offset,
            )
            batch = results.get("list") or []
            for item in batch:
                uuid = item.get("uuid")
                if not uuid:
                    continue
                index = item.get("index")
                order = float(index) if isinstance(index, (int, float)) else float("inf")
                collected.append(
                    (order, ChapterInfo(id=str(uuid), title=str(item.get("name") or ""), group=group))
                )
            total = results.get("total")
            offset += len(batch)
            if not batch or (isinstance(total, int) and offset >= total) or len(batch) < CHAPTER_PAGE_SIZE:
                break
        collected.sort(key=lambda pair: pair[0])
        return [chapter for _, chapter in collected]

    async def detail(self, comic_id: str) -> ComicDetail:
        results = await self._get_results(f"comic2/{comic_id}")
        comic = results.get("comic") or {}
        if not comic or comic.get("b_404"):
            raise NotFoundError(f"拷贝漫画未找到漫画 {comic_id}", source=self.key)

        path_word = str(comic.get("path_word") or comic_id)
        groups = results.get("groups") or {}

        chapters: list[ChapterInfo] = []
        for group_path in groups:
            group_meta = groups.get(group_path) or {}
            group_name = str(group_meta.get("name") or group_meta.get("display") or group_path)
            # comic2 里内嵌的章节常常是空的，只有章节列表接口才是完整的
            full = await self._group_chapters(path_word, group_path)
            if full:
                chapters.extend(full)
                continue
            chapters.extend(
                ChapterInfo(id=str(chapter["uuid"]), title=str(chapter.get("name") or ""), group=group_name)
                for chapter in group_meta.get("chapters") or []
                if chapter.get("uuid")
            )

        status = self._display(comic.get("status"))
        region = self._display(comic.get("region"))
        tags = {
            "作者": self._names(comic.get("author")),
            "状态": [status] if status else [],
            "题材": self._names(comic.get("theme")),
            "地区": [region] if region else [],
        }
        description = comic.get("brief")
        return ComicDetail(
            id=path_word,
            title=str(comic.get("name") or ""),
            cover=comic.get("cover"),
            description=str(description).strip() if description else None,
            update_time=comic.get("datetime_updated") or None,
            tags={key: value for key, value in tags.items() if value},
            chapters=chapters,
        )

    async def chapter(self, comic_id: str, chapter_id: str) -> ChapterImages:
        results = await self._get_results(f"comic/{comic_id}/chapter/{chapter_id}")
        chapter = results.get("chapter") or {}
        contents = chapter.get("contents") or []
        images = [str(item["url"]) for item in contents if isinstance(item, dict) and item.get("url")]
        if not images:
            if results.get("is_lock") or results.get("is_vip") or results.get("is_login"):
                raise SourceError(
                    f"拷贝漫画章节 {chapter_id} 需要登录或 VIP 权限（is_lock/is_vip/is_login）",
                    source=self.key,
                )
            raise NotFoundError(f"拷贝漫画章节 {chapter_id} 没有可用图片", source=self.key)

        return ChapterImages(
            source=self.key,
            comic_id=str(comic_id),
            chapter_id=str(chapter_id),
            title=str(chapter.get("name")) if chapter.get("name") else None,
            count=len(images),
            images=images,
        )
