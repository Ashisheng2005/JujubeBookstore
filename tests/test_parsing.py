"""解析层单元测试：全部离线，用录制的真实响应片段做 fixture。"""

from __future__ import annotations

import asyncio

import pytest

from app.errors import NotFoundError, SourceError
from app.sources.zaimanhua import (
    ZaimanhuaSource,
    normalize_chapter_title,
    resolve_comic_id,
    split_tags,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload)


class FakePool:
    def __init__(self, payload):
        self._client = FakeClient(payload)

    def client(self, *, use_proxy: bool):
        assert use_proxy is False, "再漫画不需要代理"
        return self._client


def make_source(payload) -> ZaimanhuaSource:
    return ZaimanhuaSource(FakePool(payload))


# --- 纯函数 ---------------------------------------------------------------
@pytest.mark.parametrize(
    "item,expected",
    [
        ({"id": 77856, "comic_id": 0}, "77856"),
        ({"id": 0, "comic_id": 80808}, "80808"),
        ({"id": "100", "comic_id": "200"}, "200"),
        ({"id": 0, "comic_id": 0}, ""),
    ],
)
def test_resolve_comic_id(item, expected):
    assert resolve_comic_id(item) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", "第1话"),
        ("12.5", "第12.5话"),
        ("连载3卷", "第3卷"),
        ("第1话 出发", "第1话 出发"),
        (None, ""),
    ],
)
def test_normalize_chapter_title(raw, expected):
    assert normalize_chapter_title(raw) == expected


def test_split_tags_handles_both_shapes():
    assert split_tags("冒险,热血") == ["冒险", "热血"]
    assert split_tags([{"tag_name": "冒险"}, {"tag_name": "热血"}]) == ["冒险", "热血"]
    assert split_tags(None) == []


# --- 解析链路 -------------------------------------------------------------
SEARCH_PAYLOAD = {
    "errno": 0,
    "errmsg": "",
    "data": {
        "list": [
            {
                "id": 77856,
                "comic_id": 0,
                "title": "火影忍者外传",
                "cover": "https://images.zaimanhua.com/webpic/18/a.jpg",
                "authors": "岸本齐史",
                "status": "已完结",
                "types": "冒险,热血",
                "last_update_chapter_name": "全一话",
                "last_updatetime": 1700000000,
            }
        ]
    },
}


def test_search_maps_fields():
    source = make_source(SEARCH_PAYLOAD)
    items = asyncio.run(source.search("火影", page=2))
    assert len(items) == 1
    item = items[0]
    assert item.id == "77856"
    assert item.title == "火影忍者外传"
    assert item.authors == ["岸本齐史"]
    assert item.tags == ["已完结", "冒险", "热血"]
    assert item.status == "已完结"
    assert item.description == "全一话"
    # page 参数透传给上游
    url, kwargs = source.pool.client(use_proxy=False).calls[0]
    assert url.endswith("/search/index")
    assert kwargs["params"] == {"source": 0, "keyword": "火影", "page": 2, "sort": 0, "size": 20}


DETAIL_PAYLOAD = {
    "errno": 0,
    "data": {
        "data": {
            "id": 80808,
            "title": "测试漫画",
            "cover": "https://images.zaimanhua.com/webpic/18/b.jpg",
            "description": "简介",
            "last_updatetime": 1700000000,
            "last_update_chapter_name": "第3话",
            "authors": [{"tag_name": "作者A"}],
            "status": [{"tag_name": "连载中"}],
            "types": [{"tag_name": "冒险"}],
            "chapters": [
                {"title": "连载", "data": [
                    {"chapter_id": 3, "chapter_title": "3"},
                    {"chapter_id": 2, "chapter_title": "2"},
                    {"chapter_id": 1, "chapter_title": "1"},
                ]},
                {"title": "番外", "data": [{"chapter_id": 99, "chapter_title": "特别篇"}]},
            ],
        }
    },
}


def test_detail_orders_chapters_and_maps_tags():
    source = make_source(DETAIL_PAYLOAD)
    detail = asyncio.run(source.detail("80808"))
    assert detail.id == "80808"
    assert [c.id for c in detail.chapters] == ["1", "2", "3", "99"]
    assert [c.title for c in detail.chapters[:3]] == ["第1话", "第2话", "第3话"]
    assert detail.chapters[3].group == "番外"
    assert detail.tags["作者"] == ["作者A"]
    assert detail.tags["标签"] == ["冒险"]
    assert detail.tags["状态"] == ["连载中", "第3话"]
    assert detail.update_time == "2023-11-15"
    # 必须带 _v，否则上游会按 android 渠道返回 errno=2
    url, kwargs = source.pool.client(use_proxy=False).calls[0]
    assert url.endswith("/comic/detail/80808")
    assert kwargs["params"] == {"_v": "2.2.5"}


CHAPTER_PAYLOAD = {
    "errno": 0,
    "data": {
        "data": {
            "chapter_id": 1,
            "comic_id": 80808,
            "title": "第1话",
            "page_url": ["https://images.zaimanhua.com/i/sd/1.jpg"],
            "page_url_hd": ["https://images.zaimanhua.com/i/hd/1.jpg?sign=abc&t=1"],
        }
    },
}


def test_chapter_prefers_hd_urls():
    source = make_source(CHAPTER_PAYLOAD)
    images = asyncio.run(source.chapter("80808", "1"))
    assert images.count == 1
    assert images.images == ["https://images.zaimanhua.com/i/hd/1.jpg?sign=abc&t=1"]
    assert images.source == "zaimanhua"


def test_removed_comic_raises_not_found():
    source = make_source({"errno": 2, "errmsg": "漫画不存在或被删除", "data": {}})
    with pytest.raises(NotFoundError):
        asyncio.run(source.detail("77856"))


def test_chapter_needs_login_raises_source_error():
    payload = {
        "errno": 0,
        "data": {"data": {"chapter_id": 1, "page_url_hd": [], "canRead": False}},
    }
    source = make_source(payload)
    with pytest.raises(SourceError) as excinfo:
        asyncio.run(source.chapter("80808", "1"))
    assert "需要登录" in str(excinfo.value)
