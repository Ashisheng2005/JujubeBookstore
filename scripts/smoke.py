"""端到端联调脚本：搜索 -> 详情 -> 图片直链/磁力种子 -> 真文件校验。

用法::

    python scripts/smoke.py                     # 漫画源：默认 zaimanhua，关键词 火影
    python scripts/smoke.py 海贼王 --source mangabz
    python scripts/smoke.py 海贼王 --kind resource --source dmhy
    python scripts/smoke.py 海贼王 --kind resource --source dmhy --category 漫畫

不需要启动服务，直接打站点接口，用来确认「解析逻辑没被站点改版打挂」。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.errors import SourceError  # noqa: E402
from app.http_client import HttpClientPool  # noqa: E402
from app.resources import create_resource  # noqa: E402
from app.sources import create_source  # noqa: E402

IMAGE_MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG": "png",
    b"GIF8": "gif",
    b"RIFF": "webp",
}


def sniff(content: bytes) -> str:
    for magic, name in IMAGE_MAGIC.items():
        if content.startswith(magic):
            return name
    return "unknown"


async def run(source_key: str, keyword: str, pages: int, tries: int = 6) -> int:
    settings = load_settings()
    pool = HttpClientPool(settings)
    source = create_source(source_key, pool)
    print(f"源: {source.name} ({source.key})  proxy={settings.proxy or '直连'}")

    try:
        print(f"\n[1/4] 搜索 {keyword!r}")
        items = await source.search(keyword, page=1)
        print(f"      命中 {len(items)} 条")
        for item in items[:5]:
            print(f"      - id={item.id:<8} {item.title[:34]:<36} {item.status or ''} {item.tags[:3]}")

        target = None
        print(f"\n[2/4] 详情（最多试 {pages} 条，搜索 id 可能已下架）")
        for item in items[:pages]:
            try:
                detail = await source.detail(item.id)
            except SourceError as exc:
                print(f"      id={item.id:<8} 跳过: {exc}")
                continue
            print(f"      命中 id={detail.id} 《{detail.title}》")
            print(f"      封面: {detail.cover}")
            print(f"      标签: {detail.tags}")
            print(f"      更新时间: {detail.update_time}  章节数: {len(detail.chapters)}")
            if detail.chapters:
                print(f"      首章: {detail.chapters[0].title} (group={detail.chapters[0].group})")
                print(f"      末章: {detail.chapters[-1].title}")
            target = detail
            break
        if target is None:
            print("      !! 所有候选都取不到详情，站点结构可能已变")
            return 1

        if not target.chapters:
            print("\n      !! 该漫画没有章节")
            return 1

        print(f"\n[3/4] 章节图片（可读章节可能是有条件的，逐个试前 {tries} 章）")
        images = None
        for candidate in target.chapters[:tries]:
            label = f"{candidate.title} (id={candidate.id}, group={candidate.group})"
            try:
                images = await source.chapter(target.id, candidate.id)
            except SourceError as exc:
                print(f"      {label} 跳过: {exc}")
                continue
            print(f"      命中 {label}  共 {images.count} 页")
            for url in images.images[:2]:
                print(f"      - {url[:120]}")
            break
        if images is None:
            print("      !! 前几章都拿不到图片（可能需要登录/VIP，属于站点内容策略）")
            return 1

        print("\n[4/4] 下载首图校验（前 1KB 就够看格式）")
        headers = {"Referer": source.image_referer} if source.image_referer else {}
        resp = await source.client.get(images.images[0], headers=headers)
        resp.raise_for_status()
        head = resp.content[:1024]
        print(f"      HTTP {resp.status_code}  {len(resp.content)} bytes  格式={sniff(head)}")
        print(f"      content-type={resp.headers.get('content-type')}")
        return 0
    finally:
        await pool.aclose()


async def run_resource(source_key: str, keyword: str, page: int, category: str | None) -> int:
    """资源索引源（BT/磁力）联调：搜索 -> 详情 -> 下载种子校验。"""
    settings = load_settings()
    pool = HttpClientPool(settings)
    source = create_resource(source_key, pool)
    print(f"资源源: {source.name} ({source.key})  proxy={settings.proxy or '直连'}")

    try:
        print(f"\n[1/3] 搜索 {keyword!r}  category={category or '全部'}")
        items = await source.search(keyword, page=page, category=category)
        print(f"      命中 {len(items)} 条")
        for item in items[:5]:
            magnet = (item.magnet or "")[:52]
            print(f"      - [{item.category or '?'}] {item.title[:44]}  {item.size or '?'}  {magnet}")

        if not items:
            print("      !! 没有结果")
            return 1

        target = items[0]
        print(f"\n[2/3] 详情 id={target.id}")
        detail = await source.detail(target.id)
        print(f"      标题: {detail.title[:60]}")
        print(f"      分类: {detail.category}  大小: {detail.size}  发布: {detail.published_at}")
        print(f"      磁力: {(detail.magnet or '无')[:80]}")
        print(f"      种子: {detail.torrent or '无'}")
        print(f"      文件 {len(detail.files)} 个，前两个:")
        for f in detail.files[:2]:
            print(f"        {f.name[:56]}  {f.size or ''}")

        if not detail.torrent:
            print("      !! 没有种子直链（可能该条目只提供磁力）")
            return 1

        print("\n[3/3] 下载 .torrent 校验（bencode 应以 d8:announce 开头）")
        resp = await source.client.get(detail.torrent, headers=source.site_headers)
        resp.raise_for_status()
        head = resp.content[:64]
        looks_bencode = head.startswith(b"d") and b"announce" in head
        print(f"      HTTP {resp.status_code}  {len(resp.content)} bytes  bencode={looks_bencode}")
        print(f"      头部: {head[:32]!r}")
        return 0 if looks_bencode else 1
    finally:
        await pool.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="漫画源 / 资源源端到端联调")
    parser.add_argument("keyword", nargs="?", default="火影")
    parser.add_argument("--source", default="zaimanhua")
    parser.add_argument("--kind", choices=["comic", "resource"], default="comic")
    parser.add_argument("--category", default=None, help="资源源分类，如 動畫/漫畫/游戏/3")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--pages", type=int, default=3, help="漫画：详情最多尝试的候选数")
    parser.add_argument("--tries", type=int, default=6, help="漫画：章节最多尝试的候选数")
    args = parser.parse_args()
    if args.kind == "resource":
        return asyncio.run(run_resource(args.source, args.keyword, args.page, args.category))
    return asyncio.run(run(args.source, args.keyword, args.pages, args.tries))


if __name__ == "__main__":
    raise SystemExit(main())
