"""拷贝漫画源测试（离线）：节点轮换、分组章节翻页、图片权限判定、域名白名单。"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from app.errors import NotFoundError, SourceError
from app.sources.mangacopy import PAGE_SIZE, MangacopySource


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self, router):
        self.router = router
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.router(url, kwargs.get("params") or {}))

    def params_of(self, index: int) -> dict:
        return self.calls[index][1].get("params") or {}


class FakePool:
    """拷贝漫画必须走代理，这里顺便断言它确实声明了 needs_proxy。"""

    def __init__(self, router):
        self._client = FakeClient(router)

    def client(self, *, use_proxy: bool):
        assert use_proxy is True, "拷贝漫画必须通过代理访问"
        return self._client


def make_source(router) -> MangacopySource:
    return MangacopySource(FakePool(router))


def ok(results: dict) -> dict:
    return {"code": 200, "message": "请求成功", "results": results}


def test_image_host_allowlist_covers_cdn_wildcard():
    source = make_source(lambda url, params: {})
    assert source.allows_image_host("sh.mangafunb.fun") is True
    assert source.allows_image_host("www.mangacopy.com") is True
    assert source.allows_image_host("mangafunb.fun.evil.com") is False
    assert source.allows_image_host("evil.com") is False


# --- 搜索 -----------------------------------------------------------------
SEARCH_ITEM = {
    "name": "火影忍者特典",
    "alias": "火影忍者特典,火影忍者特典",
    "path_word": "huoyingrenzhetedian",
    "cover": "https://sh.mangafunb.fun/h/huoyingrenzhetedian/cover/a.jpg",
    "author": [{"name": "岸本斉史", "path_word": "anbenqishi"}],
    "popular": 83731,
}


def test_search_maps_fields_and_paging():
    seen: list[str] = []

    def router(url, params):
        seen.append(url)
        return ok({"list": [SEARCH_ITEM], "total": 247, "limit": PAGE_SIZE, "offset": 0})

    source = make_source(router)
    items = asyncio.run(source.search("火影", page=3))
    assert len(items) == 1
    assert items[0].id == "huoyingrenzhetedian"
    assert items[0].title == "火影忍者特典"
    assert items[0].authors == ["岸本斉史"]
    assert items[0].cover.startswith("https://sh.mangafunb.fun/")

    params = source.pool.client(use_proxy=True).params_of(0)
    assert params["platform"] == 3
    assert params["limit"] == PAGE_SIZE
    assert params["offset"] == (3 - 1) * PAGE_SIZE
    assert seen[0].startswith("https://api.2024manga.com/api/v3/search/comic")


# --- 节点轮换 -------------------------------------------------------------
def test_node_error_falls_back_to_next_host():
    tried: list[str] = []

    def router(url, params):
        host = urlparse(url).netloc
        tried.append(host)
        if host == "api.2024manga.com":
            return {"code": 210, "message": "請升級到最新的APP(3.0.0)進行使用", "results": None}
        return ok({"list": [SEARCH_ITEM], "total": 1})

    source = make_source(router)
    items = asyncio.run(source.search("火影"))
    assert tried == ["api.2024manga.com", "www.mangacopy.com"]
    assert items[0].id == "huoyingrenzhetedian"


def test_all_business_error_raises_source_error():
    def router(url, params):
        return {"code": 500, "message": "服务器内部错误", "results": None}

    source = make_source(router)
    with pytest.raises(SourceError) as excinfo:
        asyncio.run(source.search("火影"))
    assert "服务器内部错误" in str(excinfo.value)


def test_latest_uses_catalog_ordering():
    """文档里的 update/newest 已 404，最近更新走 comics + ordering。"""

    def router(url, params):
        assert urlparse(url).path.endswith("/api/v3/comics")
        return ok({"list": [SEARCH_ITEM], "total": 122035})

    source = make_source(router)
    items = asyncio.run(source.latest(page=2))
    assert items[0].id == "huoyingrenzhetedian"
    params = source.pool.client(use_proxy=True).params_of(0)
    assert params["ordering"] == "-datetime_updated"
    assert params["offset"] == PAGE_SIZE


# --- 详情与分组章节翻页 ---------------------------------------------------
COMIC = {
    "name": "火影忍者特典",
    "path_word": "huoyingrenzhetedian",
    "cover": "https://sh.mangafunb.fun/cover.jpg",
    "brief": "\n   特典部分 ",
    "datetime_updated": "2018-11-05",
    "author": [{"name": "岸本斉史"}],
    "status": {"display": "已完結"},
    "theme": [{"name": "冒險"}, {"name": "熱血"}],
    "region": {"display": "日本"},
    "b_404": False,
}


def test_detail_paginates_group_chapters_and_sorts_by_index():
    def router(url, params):
        path = urlparse(url).path
        if path.endswith("/comic2/huoyingrenzhetedian"):
            # 内嵌章节为空 —— 真实情况就是这样，必须再打章节列表接口
            return ok({"comic": COMIC, "groups": {"default": {"path_word": "default", "chapters": []}}})
        if path.endswith("/group/default/chapters"):
            offset = int(params.get("offset", 0))
            if offset == 0:
                # 两页共 102 章：故意让顺序打乱，验证按 index 重排
                items = [
                    {"uuid": f"uuid-{i}", "name": f"第{i}话", "index": i}
                    for i in range(100, 0, -1)
                ]
                return ok({"list": items, "total": 102, "limit": 100, "offset": 0})
            return ok(
                {
                    "list": [
                        {"uuid": "uuid-101", "name": "第101话", "index": 101},
                        {"uuid": "uuid-102", "name": "第102话", "index": 102},
                    ],
                    "total": 102,
                    "limit": 100,
                    "offset": 100,
                }
            )
        raise AssertionError(f"未预期的请求: {url}")

    source = make_source(router)
    detail = asyncio.run(source.detail("huoyingrenzhetedian"))
    assert detail.title == "火影忍者特典"
    assert detail.description == "特典部分"
    assert detail.tags["作者"] == ["岸本斉史"]
    assert detail.tags["状态"] == ["已完結"]
    assert detail.tags["题材"] == ["冒險", "熱血"]
    assert detail.tags["地区"] == ["日本"]
    assert len(detail.chapters) == 102
    assert [c.id for c in detail.chapters[:3]] == ["uuid-1", "uuid-2", "uuid-3"]
    assert detail.chapters[-1].id == "uuid-102"
    assert all(c.group == "default" for c in detail.chapters)


def test_detail_404_raises_not_found():
    def router(url, params):
        return ok({"comic": {**COMIC, "b_404": True}, "groups": {}})

    source = make_source(router)
    with pytest.raises(NotFoundError):
        asyncio.run(source.detail("nope"))


# --- 章节图片 -------------------------------------------------------------
CHAPTER_UUID = "09a0024c-e0fb-11e8-a1df-00163e0ca5bd"


def test_chapter_returns_image_urls():
    def router(url, params):
        assert urlparse(url).path.endswith(f"/comic/huoyingrenzhetedian/chapter/{CHAPTER_UUID}")
        return ok(
            {
                "is_lock": False,
                "is_login": False,
                "chapter": {
                    "name": "皆之书",
                    "contents": [
                        {"url": "https://sh.mangafunb.fun/h/a/1.webp"},
                        {"url": "https://sh.mangafunb.fun/h/a/2.webp"},
                    ],
                },
            }
        )

    source = make_source(router)
    images = asyncio.run(source.chapter("huoyingrenzhetedian", CHAPTER_UUID))
    assert images.count == 2
    assert images.title == "皆之书"
    assert images.images[0].endswith("1.webp")


def test_locked_chapter_reports_login_required():
    def router(url, params):
        return ok({"is_lock": True, "is_vip": True, "chapter": {"contents": []}})

    source = make_source(router)
    with pytest.raises(SourceError) as excinfo:
        asyncio.run(source.chapter("huoyingrenzhetedian", CHAPTER_UUID))
    assert "VIP" in str(excinfo.value)
