"""资源索引源的 API 路由测试：用假资源源跑路由，不触网。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import NotFoundError
from app.main import app
from app.resources import register
from app.resources.base import ResourceSource
from app.schemas import ResourceDetail, ResourceFile, ResourceItem


class FakeResourceSource(ResourceSource):
    key = "fakeres"
    name = "假资源源"

    async def search(self, keyword, page=1, category=None):
        if keyword == "empty":
            return []
        return [
            ResourceItem(
                id="1001",
                title=f"{keyword}-{page}",
                category=category or "動畫",
                size="1.0GB",
                magnet="magnet:?xt=urn:btih:AAA",
                detail_url="https://share.dmhy.org/topics/view/1001_x.html",
            )
        ]

    async def detail(self, item_id):
        if item_id == "404":
            raise NotFoundError("资源不存在", source=self.key)
        return ResourceDetail(
            id=item_id,
            title="测试资源",
            magnet="magnet:?xt=urn:btih:BBB",
            torrent="https://dl.dmhy.org/x.torrent",
            files=[ResourceFile(name="a.mkv", size="1.0GB")],
        )


register(FakeResourceSource)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_resource_registry_and_info(client):
    sources = client.get("/api/resources").json()
    keys = {item["key"]: item for item in sources}
    assert "dmhy" in keys and "fakeres" in keys
    assert keys["dmhy"]["kind"] == "resource"
    assert keys["fakeres"]["name"] == "假资源源"
    # 漫画源列表不应混入资源源
    comic_keys = {item["key"] for item in client.get("/api/sources").json()}
    assert "dmhy" not in comic_keys
    assert all(item["kind"] == "comic" for item in client.get("/api/sources").json())


def test_healthz_lists_both_registries(client):
    body = client.get("/healthz").json()
    assert "dmhy" in body["resource_sources"]
    assert "zaimanhua" in body["sources"]


def test_resource_search(client):
    body = client.get("/api/resources/fakeres/search", params={"q": "海贼王", "page": 2}).json()
    assert body["count"] == 1
    assert body["items"][0]["title"] == "海贼王-2"
    assert body["items"][0]["magnet"].startswith("magnet:")


def test_resource_search_carries_category(client):
    body = client.get(
        "/api/resources/fakeres/search", params={"q": "海贼王", "category": "漫畫"}
    ).json()
    assert body["category"] == "漫畫"
    assert body["items"][0]["category"] == "漫畫"


def test_resource_search_requires_keyword(client):
    assert client.get("/api/resources/fakeres/search").status_code == 422


def test_resource_latest_not_implemented_returns_501(client):
    resp = client.get("/api/resources/fakeres/latest")
    assert resp.status_code == 501


def test_resource_detail_and_not_found(client):
    detail = client.get("/api/resources/fakeres/item/1001").json()
    assert detail["magnet"] == "magnet:?xt=urn:btih:BBB"
    assert detail["torrent"].endswith(".torrent")
    assert detail["files"][0]["name"] == "a.mkv"
    assert client.get("/api/resources/fakeres/item/404").status_code == 404


def test_unknown_resource_source_404(client):
    resp = client.get("/api/resources/nope/search", params={"q": "x"})
    assert resp.status_code == 404
    assert "未知的资源源" in resp.json()["detail"]
