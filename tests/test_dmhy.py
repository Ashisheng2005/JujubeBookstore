"""动漫花园（dmhy）资源源测试（离线）：列表列解析、分类映射、详情磁力/种子/文件列表。

HTML 片段按真实页面结构裁剪而来（选择器与线上一致）。
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from app.errors import NotFoundError, SourceError
from app.resources.dmhy import DmhySource, resolve_category

LIST_HTML = """
<table id="topic_list">
  <thead><tr><th>完成</th><th>發佈人</th></tr></thead>
  <tbody>
    <tr class="">
      <td width="98"> 2026/09/21 16:48 <span style="display: none;">2026/09/21 16:48</span></td>
      <td width="6%" align="center"><a class="sort-2" href="/topics/list/sort_id/2"><font color=red>動畫</font></a></td>
      <td class="title">
        <span class="tag"><a href="/topics/list/team_id/767">天月動漫&amp;發佈組</a></span>
        <a href="/topics/view/727506_Skymoon-Raws_One_Piece_1179_ViuTV_WEB-RIP_CHT_SRT_1080p_MKV.html" target="_blank">[Skymoon-Raws][One Piece <span class="keyword">海賊王</span>][1179][ViuTV][WEB-RIP][CHT][SRT][1080p][MKV]</a>
        <span style="color: gray;">約2條評論</span>
      </td>
      <td nowrap="nowrap" align="center"><a class="download-arrow arrow-magnet" title="磁力下載" href="magnet:?xt=urn:btih:VWOE6SU55B373U4CT3RBHQEW6X24BEBC&amp;dn=&amp;tr=http%3A%2F%2F104.143">magnet</a></td>
      <td width="5%">463.2MB</td>
      <td>12</td>
      <td>3</td>
      <td>45</td>
      <td><a href="/topics/list/user_id/730809">Laputa</a></td>
    </tr>
    <tr class="">
      <td width="98"> 2026/09/14 17:31 </td>
      <td width="6%" align="center"><a class="sort-3" href="/topics/list/sort_id/3"><font color=red>漫畫</font></a></td>
      <td class="title">
        <a href="/topics/view/727000_OPFans_ONE_PIECE_1043.html" target="_blank">[OPFans枫雪动漫][ONE PIECE海贼王][第1043话]</a>
      </td>
      <td nowrap="nowrap" align="center"><a class="download-arrow arrow-magnet" href="magnet:?xt=urn:btih:QMUN3TINIFSPFOFSIXETDIRCZO7ECZAK">magnet</a></td>
      <td width="5%">1.4GB</td>
      <td>-</td>
      <td>-</td>
      <td>-</td>
    </tr>
    <tr><td colspan="9">广告行，没有条目链接</td></tr>
  </tbody>
</table>
"""

DETAIL_HTML = """
<html><head><title>[Skymoon-Raws][One Piece 海賊王][1179] - 動漫花園</title></head><body>
<h3>[Skymoon-Raws][One Piece 海賊王][1179][ViuTV][WEB-RIP][CHT][SRT][1080p][MKV]</h3>
<div class="topic-main">
  <div class="info">
    所屬分類: <a href="/topics/list/sort_id/2">動畫</a>
    發佈時間: 2026/09/21 16:48
    種子下載: <a href="#description-end">下載種子/磁力鏈接</a>
    文件大小: 463.2MB
    訪客互動: <a href="/report/add/referer/1">舉報該資源</a>
  </div>
  <div id="tabs-1">
    會員專用連接: [Skymoon-Raws][One Piece 海賊王][1179]
    Magnet連接: <a href="magnet:?xt=urn:btih:VWOE6SU55B373U4CT3RBHQEW6X24BEBC&amp;dn=&amp;tr=http%3A%2F%2F104.143">magnet:?xt=urn:btih:VWOE...</a>
    種子下載: <a href="//dl.dmhy.org/2026/09/21/ad9c4f4a9de877fdd3829ee213c096f5f5c09022.torrent">下載種子</a>
  </div>
  <div class="file_list">
    <ul>
      <li>[Skymoon-Raws][One Piece][1179][ViuTV][WEB-RIP][CHT][SRT][1080p][AVC AAC].mkv 463.2MB</li>
      <li>readme.txt</li>
    </ul>
  </div>
</div>
</body></html>
"""


class StubPool:
    def client(self, **kwargs):  # pragma: no cover - 解析测试不触网
        raise AssertionError("解析测试不应发请求")


@pytest.fixture()
def source() -> DmhySource:
    return DmhySource(StubPool())


# --- 分类映射 -------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("3", "3"),
        (3, "3"),
        ("動畫", "2"),
        ("动画", "2"),
        ("漫画", "3"),
        ("游戏", "9"),
        ("RAW", "7"),
        ("raw", "7"),
    ],
)
def test_resolve_category(value, expected):
    assert resolve_category(value) == expected


def test_resolve_category_rejects_unknown():
    with pytest.raises(SourceError):
        resolve_category("不存在的分类")


# --- 列表解析 -------------------------------------------------------------
def test_parse_rows_extracts_all_columns(source):
    items = source._parse_rows(LIST_HTML)
    assert len(items) == 2  # 广告行被跳过

    first = items[0]
    # id 用规范 slug：dmhy 对纯数字 id 只返回空壳页
    assert first.id == "727506_Skymoon-Raws_One_Piece_1179_ViuTV_WEB-RIP_CHT_SRT_1080p_MKV.html"
    assert first.title.startswith("[Skymoon-Raws][One Piece 海賊王]")
    assert first.category == "動畫"
    assert first.size == "463.2MB"
    assert first.published_at == "2026/09/21 16:48"
    assert first.publisher == "天月動漫&發佈組"
    assert first.uploader == "Laputa"
    assert first.magnet.startswith("magnet:?xt=urn:btih:VWOE6SU55B373U4CT3RBHQEW6X24BEBC")
    assert first.seeders == "12"
    assert first.leechers == "3"
    assert first.completed == "45"
    assert first.comments == 2
    assert first.detail_url.endswith("727506_Skymoon-Raws_One_Piece_1179_ViuTV_WEB-RIP_CHT_SRT_1080p_MKV.html")

    second = items[1]
    assert second.id == "727000_OPFans_ONE_PIECE_1043.html"
    assert second.category == "漫畫"
    assert second.size == "1.4GB"
    assert second.publisher is None
    assert second.uploader is None
    assert second.seeders is None  # "-" 归一化成 None
    assert second.comments is None


# --- 请求参数 -------------------------------------------------------------
def test_search_passes_keyword_page_and_category(source, monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_get(path, **params):
        calls.append((path, params))
        return LIST_HTML

    monkeypatch.setattr(source, "_get_html", fake_get)
    items = asyncio.run(source.search("海贼王", page=2, category="漫画"))
    assert len(items) == 2
    path, params = calls[0]
    assert path == "/topics/list/page/2"
    assert params == {"keyword": "海贼王", "sort_id": "3"}


def test_latest_omits_keyword(source, monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_get(path, **params):
        calls.append((path, params))
        return LIST_HTML

    monkeypatch.setattr(source, "_get_html", fake_get)
    asyncio.run(source.latest(page=1, category="動畫"))
    assert calls[0][0] == "/topics/list"
    assert calls[0][1] == {"sort_id": "2"}


# --- 详情解析 -------------------------------------------------------------
def test_parse_detail_extracts_magnet_torrent_and_files(source, monkeypatch):
    async def fake_get(path, **params):
        assert path == "/topics/view/727506_Skymoon-Raws_One_Piece_1179_ViuTV_WEB-RIP_CHT_SRT_1080p_MKV.html"
        return DETAIL_HTML

    monkeypatch.setattr(source, "_get_html", fake_get)
    detail = asyncio.run(
        source.detail("727506_Skymoon-Raws_One_Piece_1179_ViuTV_WEB-RIP_CHT_SRT_1080p_MKV.html")
    )
    assert detail.id == "727506_Skymoon-Raws_One_Piece_1179_ViuTV_WEB-RIP_CHT_SRT_1080p_MKV.html"
    assert detail.title.startswith("[Skymoon-Raws][One Piece 海賊王]")
    assert detail.category == "動畫"
    assert detail.size == "463.2MB"
    assert detail.published_at == "2026/09/21 16:48"
    assert detail.magnet.startswith("magnet:?xt=urn:btih:VWOE6SU55B373U4CT3RBHQEW6X24BEBC")
    # 协议相对地址要补成 https
    assert detail.torrent == "https://dl.dmhy.org/2026/09/21/ad9c4f4a9de877fdd3829ee213c096f5f5c09022.torrent"
    assert [f.name for f in detail.files][0].startswith("[Skymoon-Raws][One Piece][1179]")
    assert detail.files[0].size == "463.2MB"
    assert detail.files[1].name == "readme.txt"
    assert detail.info["所屬分類"] == "動畫"


def test_detail_rejects_bare_numeric_id_with_hint(source, monkeypatch):
    """纯数字 id 会拿到空壳页，所以要提前给出可操作的报错。"""

    async def fake_get(path, **params):  # pragma: no cover - 不应该发请求
        raise AssertionError("纯数字 id 不应发起请求")

    monkeypatch.setattr(source, "_get_html", fake_get)
    with pytest.raises(NotFoundError) as excinfo:
        asyncio.run(source.detail("727506"))
    assert "slug" in str(excinfo.value)


def test_detail_accepts_full_url(source, monkeypatch):
    seen: list[str] = []

    async def fake_get(path, **params):
        seen.append(path)
        return DETAIL_HTML

    monkeypatch.setattr(source, "_get_html", fake_get)
    detail = asyncio.run(
        source.detail("https://share.dmhy.org/topics/view/727506_Skymoon-Raws_One_Piece_1179_ViuTV_WEB-RIP_CHT_SRT_1080p_MKV.html")
    )
    assert seen == ["/topics/view/727506_Skymoon-Raws_One_Piece_1179_ViuTV_WEB-RIP_CHT_SRT_1080p_MKV.html"]
    assert detail.id.endswith(".html")


def test_detail_raises_not_found_for_broken_page(source, monkeypatch):
    async def fake_get(path, **params):
        return "<html><body>这里是空壳页，没有 h3</body></html>"

    monkeypatch.setattr(source, "_get_html", fake_get)
    with pytest.raises(NotFoundError):
        asyncio.run(source.detail("999999_whatever.html"))
