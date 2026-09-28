"""Mangabz 源（www.mangabz.com）。

中文汉化漫画站，国内可直连。站点是 HTML + 一个返回打包 JS 的图片接口：

* 搜索   ``/search?title=&page=``            -> ``.mh-list li``
* 列表   ``/manga-list-0-1-10-p{page}/``     -> ``.mh-list li``
* 详情   ``/{mid}bz/``                       -> ``#chapterlistload a``
* 图片   ``/m{cid}/chapterimage.ashx?cid=&page=`` -> Dean Edwards 打包 JS（见 packer.py）

要点（改版时优先怀疑）：

* 详情页与章节页都需要 ``Referer`` 与 ``mangabz_lang=2``（简体）cookie；
* 图片接口一次返回 2 页（服务端有缓存时可能一次返回 15 页），所以要按返回数量步进翻页；
* 章节顺序在页面上是「新 -> 旧」，这里反转成阅读顺序。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from ..errors import NotFoundError, SourceError
from ..schemas import ChapterImages, ChapterInfo, ComicDetail, ComicSummary
from .base import ComicSource
from .packer import unpack

_COMIC_ID_RE = re.compile(r"/(\d+)bz/?", re.I)
_CHAPTER_ID_RE = re.compile(r"/m(\d+)/?", re.I)
_IMAGE_COUNT_RE = re.compile(r"MANGABZ_IMAGE_COUNT\s*=\s*(\d+)")
_IMAGE_CID_RE = re.compile(r"MANGABZ_CID\s*=\s*(\d+)")
_PIX_PREFIX_RE = re.compile(r'pix\s*=\s*"([^"]*)"')
_PATH_ARRAY_RE = re.compile(r"\[[^\]]*\]")
_ABS_IMAGE_RE = re.compile(r'https?://[^"\'\s,\]]+\.(?:jpg|jpeg|png|webp)[^"\'\s,\]]*', re.I)

#: 单章最多翻多少页（防止上游异常时死循环）
_MAX_IMAGE_PAGES = 500


class MangabzSource(ComicSource):
    key = "mangabz"
    name = "Mangabz"
    needs_proxy = False
    image_hosts = ("mangabz.com",)
    image_referer = "https://www.mangabz.com/"

    base_url = "https://www.mangabz.com"
    #: 站点对桌面浏览器返回完整页面，移动端 UA 会拿到不同模板，所以这里单独指定
    site_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Cookie": "mangabz_lang=2",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.mangabz.com/",
    }

    # --- 底层请求 -----------------------------------------------------
    async def _get_text(self, url: str, *, ajax: bool = False, referer: str | None = None) -> str:
        headers = dict(self.site_headers)
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        try:
            resp = await self.client.get(url, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise NotFoundError(f"Mangabz 资源不存在: {url}", source=self.key) from exc
            raise SourceError(f"Mangabz 返回 HTTP {exc.response.status_code}", source=self.key) from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"请求 Mangabz 失败: {exc}", source=self.key) from exc
        return resp.text

    # --- 列表解析 -----------------------------------------------------
    def _parse_list(self, html: str) -> list[ComicSummary]:
        soup = BeautifulSoup(html, "lxml")
        comics: list[ComicSummary] = []
        seen: set[str] = set()
        for li in soup.select(".mh-list li"):
            link = li.find("a")
            if link is None:
                continue
            match = _COMIC_ID_RE.search(str(link.get("href") or ""))
            if match is None:
                continue
            comic_id = match.group(1)
            if comic_id in seen:
                continue
            seen.add(comic_id)

            title_node = li.select_one("h2, .title") or link
            title = (link.get("title") or title_node.get_text(strip=True) or comic_id).strip()
            cover_node = li.find("img")
            cover = None
            if cover_node is not None:
                cover = cover_node.get("data-src") or cover_node.get("src")
            comics.append(
                ComicSummary(
                    id=comic_id,
                    title=title,
                    cover=self._absolute(cover),
                    tags=[tag.get_text(strip=True) for tag in li.select(".mh-list-tag, .tag") if tag.get_text(strip=True)],
                )
            )
        return comics

    def _absolute(self, href: Any) -> str | None:
        if not href:
            return None
        url = str(href).strip()
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self.base_url + url
        return url.replace("http://", "https://", 1) if url.startswith("http://") else url

    # --- 能力实现 -----------------------------------------------------
    async def search(self, keyword: str, page: int = 1) -> list[ComicSummary]:
        url = f"{self.base_url}/search?title={quote(keyword)}&page={page}"
        html = await self._get_text(url, referer=f"{self.base_url}/")
        return self._parse_list(html)

    async def latest(self, page: int = 1) -> list[ComicSummary]:
        html = await self._get_text(f"{self.base_url}/manga-list-0-1-10-p{page}/")
        return self._parse_list(html)

    async def detail(self, comic_id: str) -> ComicDetail:
        comic_id = str(comic_id).removesuffix("bz").rstrip("/")
        html = await self._get_text(f"{self.base_url}/{comic_id}bz/")
        soup = BeautifulSoup(html, "lxml")

        title_node = soup.select_one(".detail-info-title") or soup.select_one("h1")
        if title_node is None:
            raise NotFoundError(f"Mangabz 未找到漫画 {comic_id}", source=self.key)
        title = title_node.get_text(strip=True)

        cover_node = soup.select_one(".detail-info-cover") or soup.find("img")
        cover = None
        if cover_node is not None:
            cover = self._absolute(cover_node.get("data-src") or cover_node.get("src"))

        tags: dict[str, list[str]] = {}
        for span in soup.select(".detail-info-tip > span"):
            text = span.get_text(" ", strip=True)
            if "：" in text:
                key, _, value = text.partition("：")
                value = value.strip()
                if value:
                    tags.setdefault(key.strip(), []).append(value)

        description_node = soup.select_one(".detail-info-content")
        description = description_node.get_text(" ", strip=True) if description_node is not None else None

        chapters: list[ChapterInfo] = []
        for link in soup.select("#chapterlistload a"):
            match = _CHAPTER_ID_RE.search(str(link.get("href") or ""))
            if match is None:
                continue
            name = link.get_text(" ", strip=True) or match.group(1)
            chapters.append(ChapterInfo(id=match.group(1), title=name))
        chapters.reverse()  # 页面为新 -> 旧

        return ComicDetail(
            id=comic_id,
            title=title,
            cover=cover,
            description=description,
            tags=tags,
            chapters=chapters,
        )

    async def chapter(self, comic_id: str, chapter_id: str) -> ChapterImages:
        chapter_url = f"{self.base_url}/m{chapter_id}/"
        page_html = await self._get_text(chapter_url, referer=f"{self.base_url}/{str(comic_id).removesuffix('bz')}bz/")
        cid_match = _IMAGE_CID_RE.search(page_html)
        cid = cid_match.group(1) if cid_match else str(chapter_id)
        count_match = _IMAGE_COUNT_RE.search(page_html)
        expected = int(count_match.group(1)) if count_match else None

        images: list[str] = []
        seen: set[str] = set()
        page = 1
        while page <= _MAX_IMAGE_PAGES:
            api = f"{self.base_url}/m{cid}/chapterimage.ashx?cid={cid}&page={page}"
            body = await self._get_text(api, ajax=True, referer=chapter_url)
            batch = self._extract_images(body)
            fresh = [url for url in batch if url not in seen]
            if not fresh:
                break
            for url in fresh:
                seen.add(url)
                images.append(url)
            if expected is not None and len(images) >= expected:
                break
            page += len(batch)
        if expected is not None:
            images = images[:expected]
        if not images:
            raise NotFoundError(f"Mangabz 章节 {chapter_id} 没有可用图片", source=self.key)

        return ChapterImages(
            source=self.key,
            comic_id=str(comic_id).removesuffix("bz"),
            chapter_id=str(chapter_id),
            title=None,
            count=len(images),
            images=images,
        )

    # --- 图片解包 -----------------------------------------------------
    def _extract_images(self, body: str) -> list[str]:
        """``chapterimage.ashx`` 返回打包 JS，解包后形如 ``pix="前缀"; d=["1.jpg", ...]``。"""
        if not body or not body.strip():
            return []
        script = unpack(body)
        prefix_match = _PIX_PREFIX_RE.search(script)
        if prefix_match is not None:
            prefix = prefix_match.group(1)
            array_match = _PATH_ARRAY_RE.search(script[prefix_match.end():])
            if array_match is not None:
                paths = re.findall(r'"([^"]*)"', array_match.group(0))
                if paths:
                    return [prefix + path for path in paths if path]
            return [prefix + path for path in re.findall(r'"([^"]+\.(?:jpg|jpeg|png|webp))"', script, re.I)]
        # 兜底：直接抽绝对地址
        return [url.replace("http://", "https://", 1) for url in _ABS_IMAGE_RE.findall(script)]
