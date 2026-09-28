"""端到端联调脚本：搜索 -> 详情 -> 章节图片直链 -> 真图下载校验。

用法::

    python scripts/smoke.py                 # 默认源 zaimanhua，关键词 火影
    python scripts/smoke.py 斗破苍穹
    python scripts/smoke.py 火影 --source zaimanhua --pages 3

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


def main() -> int:
    parser = argparse.ArgumentParser(description="漫画源端到端联调")
    parser.add_argument("keyword", nargs="?", default="火影")
    parser.add_argument("--source", default="zaimanhua")
    parser.add_argument("--pages", type=int, default=3, help="详情最多尝试的候选数")
    parser.add_argument("--tries", type=int, default=6, help="章节最多尝试的候选数")
    args = parser.parse_args()
    return asyncio.run(run(args.source, args.keyword, args.pages, args.tries))


if __name__ == "__main__":
    raise SystemExit(main())
