"""动漫花园（share.dmhy.org）资源索引源。

**免费、公开的 BT 索引站**：浏览与搜索都不需要账号（实测匿名即可拿到 80 条/页），
交付物是 **magnet / .torrent**，没有章节与图片直链，所以走 ``ResourceSource``
与 ``/api/resources/...``，不污染漫画源抽象。

实测结构（改版时优先怀疑这里）：

* 搜索  ``/topics/list/page/{page}?keyword=&sort_id=``
* 最新  ``/topics/list`` 与 ``/topics/list/page/{page}``
* 详情  ``/topics/view/{id}_{slug}.html``：磁力在 ``a[href^=magnet:]``，
  种子在 ``a[href*=".torrent"]``（协议相对地址 ``//dl.dmhy.org/...``），
  文件列表在 ``.file_list li``，元信息在 ``.info``
* 列表列序：日期 | 分类 | 标题+发布组 | 磁力 | 大小 | 做種 | 下載 | 完成 | 發布人
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from ..errors import NotFoundError, SourceError
from ..schemas import ResourceDetail, ResourceFile, ResourceItem
from .base import ResourceSource

#: 站点分类（sort_id），来自首页导航
CATEGORIES: dict[str, int] = {
    "其他": 1,
    "動畫": 2,
    "漫畫": 3,
    "音樂": 4,
    "日劇": 6,
    "ＲＡＷ": 7,
    "遊戲": 9,
    "特攝": 12,
    "流行音樂": 15,
    "電腦遊戲": 17,
    "電視遊戲": 18,
    "掌機遊戲": 19,
    "網絡遊戲": 20,
    "遊戲周邊": 21,
    "季度全集": 31,
    "港台原版": 41,
    "日文原版": 42,
    "動漫音樂": 43,
    "同人音樂": 44,
}

#: 简体/常用别名 -> 站点繁体分类名
CATEGORY_ALIASES: dict[str, str] = {
    "动画": "動畫",
    "漫画": "漫畫",
    "音乐": "音樂",
    "日剧": "日劇",
    "游戏": "遊戲",
    "特摄": "特攝",
    "raw": "ＲＡＷ",
    "动漫音乐": "動漫音樂",
    "同人音乐": "同人音樂",
    "流行音乐": "流行音樂",
}

_VIEW_RE = re.compile(r"^/topics/view/(\d+)_")
_DATE_RE = re.compile(r"\d{4}/\d{2}/\d{2}(\s+\d{2}:\d{2})?")
_COMMENTS_RE = re.compile(r"(\d+)\s*條評論")
_SIZE_RE = re.compile(r"^(?P<name>.+?)\s+(?P<size>[\d.]+\s*(?:[KMGTP]i?B|B))$", re.I)


def resolve_category(value: str | int | None) -> str | None:
    """把分类名（简体/繁体）或数字 id 统一成 ``sort_id``。"""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text.isdigit():
        return text
    name = text if text in CATEGORIES else CATEGORY_ALIASES.get(text) or CATEGORY_ALIASES.get(text.lower())
    if name is None or name not in CATEGORIES:
        raise SourceError(f"未知分类 {value!r}，可用: {', '.join(CATEGORIES)}")
    return str(CATEGORIES[name])


class DmhySource(ResourceSource):
    key = "dmhy"
    name = "动漫花园"
    needs_proxy = False
    #: 本机实测（无代理）：直连 ConnectTimeout；强制 IPv4 通但 23~25s；走 Clash 代理约 10s。
    #: 该域名的 A 记录被污染（解析到 157.240.20.18 / 108.160.167.174 这类无关地址），
    #: AAAA 也是伪地址，所以配了代理优先走代理，没代理时用 IPv4 兜底。
    prefer_proxy = True
    prefer_ipv4 = True

    base_url = "https://share.dmhy.org"
    site_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://share.dmhy.org/",
    }

    # --- 底层请求 -----------------------------------------------------
    async def _get_html(self, path: str, **params: Any) -> str:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            resp = await self.client.get(url, params=params or None, headers=self.site_headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise NotFoundError(f"动漫花园资源不存在: {url}", source=self.key) from exc
            raise SourceError(f"动漫花园返回 HTTP {exc.response.status_code}", source=self.key) from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"请求动漫花园失败: {exc}", source=self.key) from exc
        return resp.text

    # --- 解析 ---------------------------------------------------------
    @staticmethod
    def _dash(value: Any) -> str | None:
        text = ("" if value is None else str(value)).strip()
        return None if text in {"", "-", "—"} else text

    @staticmethod
    def _text(node: Any) -> str:
        """取纯文本：不加分隔符再压缩空白。

        标题里常内嵌 ``<span class="keyword">`` 高亮节点，用默认的空格分隔符会把
        ``海賊王][1179]`` 变成 ``海賊王 ][1179]``；但也**不能**用 ``strip=True``，
        那会把 ``One Piece  海賊王`` 之间的空格吃掉。所以取原文本再统一压缩空白。
        """
        return re.sub(r"\s+", " ", node.get_text("")).strip()

    def _parse_rows(self, html: str) -> list[ResourceItem]:
        soup = BeautifulSoup(html, "lxml")
        items: list[ResourceItem] = []
        for row in soup.select("table#topic_list tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 5:
                continue
            link = cells[2].find("a", href=_VIEW_RE)
            if link is None:
                continue
            href = str(link.get("href") or "")
            # 详情页必须带 slug：/topics/view/727506  这种纯 id 地址会返回一个 9KB 的空壳页
            # （HTTP 200 但没有 h3），所以 id 直接用规范 slug，客户端才能一步取到详情。
            slug = href.removeprefix("/topics/view/")
            date_match = _DATE_RE.search(cells[0].get_text(" ", strip=True))
            comment_match = _COMMENTS_RE.search(cells[2].get_text(" ", strip=True))
            magnet = row.find("a", href=re.compile(r"^magnet:", re.I))
            team = cells[2].select_one("span.tag a")
            uploader = cells[8].find("a") if len(cells) > 8 else None
            items.append(
                ResourceItem(
                    id=slug,
                    title=self._text(link),
                    category=self._dash(cells[1].get_text(strip=True)),
                    size=self._dash(cells[4].get_text(strip=True)),
                    published_at=date_match.group(0) if date_match else None,
                    publisher=self._dash(team.get_text(" ", strip=True)) if team else None,
                    uploader=self._dash(uploader.get_text(" ", strip=True)) if uploader else None,
                    magnet=str(magnet.get("href")) if magnet else None,
                    seeders=self._dash(cells[5].get_text(strip=True)) if len(cells) > 5 else None,
                    leechers=self._dash(cells[6].get_text(strip=True)) if len(cells) > 6 else None,
                    completed=self._dash(cells[7].get_text(strip=True)) if len(cells) > 7 else None,
                    comments=int(comment_match.group(1)) if comment_match else None,
                    detail_url=f"{self.base_url}{href}",
                )
            )
        return items

    def _parse_detail(self, html: str, path: str) -> ResourceDetail:
        soup = BeautifulSoup(html, "lxml")
        title_node = soup.find("h3")
        if title_node is None:
            raise NotFoundError(f"动漫花园资源页结构异常: {path}", source=self.key)

        info_node = soup.select_one(".info")
        info: dict[str, str] = {}
        if info_node is not None:
            text = re.sub(r"\s+", " ", info_node.get_text(" ", strip=True))
            # 值里可能自带冒号（如 發佈時間 16:48），所以用「下一个字段名」作为结束边界
            boundary = "|".join(
                ("所屬分類", "發佈時間", "文件大小", "種子下載", "在线播放", "迅雷下載", "訪客互動", "另類分享")
            )
            for field in ("所屬分類", "發佈時間", "文件大小"):
                match = re.search(rf"{field}\s*[:：]\s*(.*?)(?=\s*(?:{boundary})\s*[:：]|$)", text)
                if match and match.group(1).strip():
                    info[field] = match.group(1).strip()

        magnet = soup.find("a", href=re.compile(r"^magnet:", re.I))
        torrent = soup.find("a", href=re.compile(r"\.torrent($|\?)", re.I))
        torrent_url = None
        if torrent is not None:
            href = str(torrent.get("href"))
            torrent_url = f"https:{href}" if href.startswith("//") else href

        files: list[ResourceFile] = []
        for node in soup.select(".file_list li"):
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            match = _SIZE_RE.match(text)
            if match:
                files.append(ResourceFile(name=match.group("name").strip(), size=match.group("size")))
            elif text:
                files.append(ResourceFile(name=text))

        return ResourceDetail(
            id=path.rsplit("/", 1)[-1],
            title=self._text(title_node),
            category=info.get("所屬分類"),
            size=info.get("文件大小"),
            published_at=info.get("發佈時間"),
            magnet=str(magnet.get("href")) if magnet else None,
            torrent=torrent_url,
            detail_url=f"{self.base_url}{path}",
            info=info,
            files=files,
        )

    # --- 能力实现 -----------------------------------------------------
    def _list_path(self, page: int) -> str:
        return "/topics/list" if page <= 1 else f"/topics/list/page/{page}"

    async def search(
        self, keyword: str, page: int = 1, category: str | None = None
    ) -> list[ResourceItem]:
        params: dict[str, Any] = {}
        if keyword:
            params["keyword"] = keyword
        sort_id = resolve_category(category)
        if sort_id:
            params["sort_id"] = sort_id
        html = await self._get_html(self._list_path(page), **params)
        return self._parse_rows(html)

    async def latest(self, page: int = 1, category: str | None = None) -> list[ResourceItem]:
        return await self.search("", page=page, category=category)

    async def detail(self, item_id: str) -> ResourceDetail:
        item_id = str(item_id).strip()
        if item_id.startswith("http"):
            path = urlparse(item_id).path
        elif item_id.startswith("/"):
            path = item_id
        elif item_id.endswith(".html"):
            path = f"/topics/view/{item_id}"
        elif "_" in item_id:
            path = f"/topics/view/{item_id}.html"
        elif item_id.isdigit():
            # 站点对 /topics/view/{纯数字} 返回 HTTP 200 的空壳页，必须带 slug
            raise NotFoundError(
                f"动漫花园详情需要带 slug 的规范地址，请使用列表项里的 id"
                f"（形如 727506_xxx.html）或 detail_url，而不是纯数字 {item_id}",
                source=self.key,
            )
        else:
            path = f"/topics/view/{item_id}"
        html = await self._get_html(path)
        return self._parse_detail(html, path)
