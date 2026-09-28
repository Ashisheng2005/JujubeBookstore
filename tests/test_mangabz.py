"""Mangabz 源解析测试（离线）：packer 解包 + HTML 解析。

``tests/fixtures/mangabz_chapter_page1.js`` 是真实抓取的 ``chapterimage.ashx`` 响应。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.sources.mangabz import MangabzSource
from app.sources.packer import unpack

FIXTURE = Path(__file__).parent / "fixtures" / "mangabz_chapter_page1.js"


class StubPool:
    def client(self, **kwargs):  # pragma: no cover - 解析测试不触网
        raise AssertionError("解析测试不应发请求")


@pytest.fixture()
def source() -> MangabzSource:
    return MangabzSource(StubPool())


def test_unpack_real_packed_body():
    script = unpack(FIXTURE.read_text(encoding="utf-8"))
    assert "dm5imagefun" in script
    assert 'pix="https://image.mangabz.com/1/139/29397"' in script
    # 打包内容里的路径表已被还原
    assert "/1_5790.jpg" in script


def test_extract_images_from_real_body(source):
    images = source._extract_images(FIXTURE.read_text(encoding="utf-8"))
    assert images == [
        "https://image.mangabz.com/1/139/29397/1_5790.jpg",
        "https://image.mangabz.com/1/139/29397/2_7019.jpg",
    ]


def test_unpack_passthrough_for_plain_script():
    plain = "var a = 1;"
    assert unpack(plain) == plain


def test_extract_images_handles_empty_body(source):
    assert source._extract_images("") == []
    assert source._extract_images("   ") == []


LIST_HTML = """
<ul class="mh-list">
  <li><a href="https://css.mangabz.com/logo.png"><img src="https://css.mangabz.com/logo.png"></a></li>
  <li>
    <a href="/139bz/" title="海贼王"><img data-src="https://cover.mangabz.com/1/139/a.jpg"></a>
    <h2>海贼王</h2>
  </li>
  <li>
    <a href="/24314bz/"><img src="/img/b.jpg"></a>
    <h2>海賊王yellow</h2>
  </li>
  <li><a href="/139bz/" title="海贼王"><img data-src="https://cover.mangabz.com/1/139/a.jpg"></a></li>
</ul>
"""


def test_parse_list_skips_non_comic_links_and_dedupes(source):
    comics = source._parse_list(LIST_HTML)
    assert [c.id for c in comics] == ["139", "24314"]
    assert comics[0].title == "海贼王"
    assert comics[0].cover == "https://cover.mangabz.com/1/139/a.jpg"
    # 相对路径补全
    assert comics[1].cover == "https://www.mangabz.com/img/b.jpg"


DETAIL_HTML = """
<div class="detail-info">
  <img src="https://cover.mangabz.com/1/139/cover.jpg" class="detail-info-cover">
  <p class="detail-info-title"> 海贼王</p>
  <p class="detail-info-tip">
    <span>作者：<a href="/search?title=尾田">尾田荣一郎</a></span>
    <span>状态：<span>连载中</span></span>
    <span>题材：热血</span>
  </p>
  <p class="detail-info-content">海贼王 - 尾田荣一郎</p>
</div>
<div id="chapterlistload">
  <a href="/m29399/">第2话 （18P）</a>
  <a href="/m29397/">俘虏X海贼 （20P）</a>
</div>
"""


def test_detail_parses_chapters_in_reading_order(source, monkeypatch):
    async def fake_text(url, **kwargs):
        assert url.endswith("/139bz/")
        return DETAIL_HTML

    monkeypatch.setattr(source, "_get_text", fake_text)
    detail = asyncio.run(source.detail("139bz"))
    assert detail.title == "海贼王"
    assert detail.cover == "https://cover.mangabz.com/1/139/cover.jpg"
    assert [c.id for c in detail.chapters] == ["29397", "29399"]
    assert detail.chapters[0].title == "俘虏X海贼 （20P）"
    assert detail.tags["作者"] == ["尾田荣一郎"]
    assert detail.tags["状态"] == ["连载中"]
    assert detail.description == "海贼王 - 尾田荣一郎"


def test_chapter_steps_pages_by_returned_batch(source, monkeypatch):
    """图片接口一次返回 2 页，翻页要按实际返回数量步进（1 -> 3），不要按 1 递增。"""
    calls: list[int] = []

    async def fake_text(url, **kwargs):
        if "chapterimage" not in url:
            return "var MANGABZ_CID=29397; var MANGABZ_IMAGE_COUNT=3;"
        page = int(url.rsplit("page=", 1)[1])
        calls.append(page)
        return f"PAGE {page}"

    def fake_extract(body: str) -> list[str]:
        page = int(body.split()[1])
        table = {
            1: ["https://image.mangabz.com/a/1.jpg", "https://image.mangabz.com/a/2.jpg"],
            3: ["https://image.mangabz.com/a/3.jpg"],
        }
        return table.get(page, [])

    monkeypatch.setattr(source, "_get_text", fake_text)
    monkeypatch.setattr(source, "_extract_images", fake_extract)

    images = asyncio.run(source.chapter("139", "29397"))
    assert calls == [1, 3]
    assert images.count == 3
    assert images.images[-1].endswith("/3.jpg")
