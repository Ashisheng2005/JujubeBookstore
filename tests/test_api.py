"""API 层测试：用假源跑路由，不触网。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import NotFoundError, ProxyRequiredError
from app.main import app
from app.schemas import ChapterImages, ChapterInfo, ComicDetail, ComicSummary
from app.sources import register
from app.sources.base import ComicSource


class FakeSource(ComicSource):
    key = "fake"
    name = "假源"
    needs_proxy = False
    image_hosts = ("img.example.com",)
    image_referer = "https://example.com/"

    async def search(self, keyword, page=1):
        if keyword == "boom":
            raise NotFoundError("没有结果", source=self.key)
        return [ComicSummary(id="1", title=f"{keyword}-{page}")]

    async def latest(self, page=1):
        return [ComicSummary(id="2", title=f"latest-{page}")]

    async def detail(self, comic_id):
        if comic_id == "404":
            raise NotFoundError("漫画不存在", source=self.key)
        return ComicDetail(
            id=comic_id,
            title="测试",
            chapters=[ChapterInfo(id="9", title="第9话", group="连载")],
        )

    async def chapter(self, comic_id, chapter_id):
        return ChapterImages(
            source=self.key,
            comic_id=comic_id,
            chapter_id=chapter_id,
            title="第9话",
            count=1,
            images=["https://img.example.com/1.jpg"],
        )


class ProxySource(FakeSource):
    key = "needsproxy"
    name = "需要代理的源"
    needs_proxy = True

    async def search(self, keyword, page=1):
        self.client  # 取客户端即触发代理检查，不发真实请求
        return await super().search(keyword, page=page)


register(FakeSource)
register(ProxySource)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_healthz_and_sources(client):
    assert client.get("/healthz").json()["status"] == "ok"
    keys = [item["key"] for item in client.get("/api/sources").json()]
    assert {"zaimanhua", "fake", "needsproxy"} <= set(keys)


def test_search_ok(client):
    body = client.get("/api/fake/search", params={"q": "火影", "page": 2}).json()
    assert body["count"] == 1
    assert body["items"][0]["title"] == "火影-2"


def test_search_requires_keyword(client):
    assert client.get("/api/fake/search").status_code == 422


def test_unknown_source_404(client):
    resp = client.get("/api/nope/search", params={"q": "x"})
    assert resp.status_code == 404
    assert "未知的漫画源" in resp.json()["detail"]


def test_detail_and_not_found(client):
    assert client.get("/api/fake/comic/1").json()["chapters"][0]["id"] == "9"
    assert client.get("/api/fake/comic/404").status_code == 404


def test_chapter_route(client):
    body = client.get("/api/fake/comic/1/chapter/9").json()
    assert body["images"] == ["https://img.example.com/1.jpg"]


def test_latest_route(client):
    body = client.get("/api/fake/latest", params={"page": 3}).json()
    assert body["items"][0]["title"] == "latest-3"


def test_proxy_required_maps_to_503(client, monkeypatch):
    monkeypatch.delenv("JUJUBE_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    app.state.pool = None  # 强制按当前环境重建配置
    resp = client.get("/api/needsproxy/search", params={"q": "x"})
    assert resp.status_code == 503
    assert "JUJUBE_PROXY" in resp.json()["detail"]


def test_image_proxy_rejects_foreign_host(client):
    resp = client.get("/api/fake/image", params={"url": "https://evil.example.net/a.jpg"})
    assert resp.status_code == 403


def test_image_proxy_rejects_bad_scheme(client):
    resp = client.get("/api/fake/image", params={"url": "file:///etc/passwd"})
    assert resp.status_code == 400
