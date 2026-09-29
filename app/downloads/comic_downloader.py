from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from ..sources import create_source
from .filenames import contained_path, safe_filename
from .http_downloader import DownloadError, checksum, download_http


def image_extension(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(32)
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}:
        return ".avif"
    if header.startswith(b"BM"):
        return ".bmp"
    raise DownloadError("Upstream did not return a supported image")


def pack_cbz(destination: Path, images: list[Path]):
    temporary = destination.with_name(destination.name + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in images:
            archive.write(path, arcname=path.name)
    os.replace(temporary, destination)


async def download_comic(context):
    task, repo = context.task, context.manager.repo
    source = create_source(task["source"], context.manager.pool)
    chapter = await source.chapter(
        task["payload"]["comic_id"], task["payload"]["chapter_id"]
    )
    await context.checkpoint()
    if not chapter.images:
        raise DownloadError("Chapter has no readable images")
    files = await repo.files(task["id"])
    by_page = {record["name"].split(".")[0]: record for record in files}
    paths: list[Path | None] = [None] * len(chapter.images)
    pending = []
    for index, url in enumerate(chapter.images):
        page = f"{index + 1:03d}"
        old = by_page.get(page)
        valid = False
        if old:
            path = contained_path(context.manager.root, old["path"])
            valid = (
                path.is_file()
                and old["status"] == "completed"
                and path.stat().st_size == old["size"]
                and old["checksum"] == await asyncio.to_thread(checksum, path)
            )
            if valid:
                paths[index] = path
                context.file_bytes[index] = old["size"]
                context.file_totals[index] = old["size"]
                context.done.add(index)
        if not valid:
            ext = Path(urlparse(url).path).suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}:
                ext = ".jpg"
            name = page + ext
            path = context.directory / "pages" / name
            await repo.save_file(task["id"], name, context.relative(path))
            if old and old["name"] != name:
                await repo.remove_file_record(task["id"], old["name"])
            pending.append((index, url, path))
    context.file_count = len(chapter.images)
    await context.report(force=True)

    async def page_worker():
        while pending:
            await context.checkpoint()
            index, url, target = pending.pop(0)

            async def progress(downloaded, total):
                context.file_bytes[index] = downloaded
                context.file_totals[index] = total
                await context.report()

            result = await download_http(
                source.client,
                url,
                target,
                allows_host=source.allows_image_host,
                checkpoint=context.checkpoint,
                progress=progress,
                resume=False,
                headers={
                    **getattr(source, "site_headers", {}),
                    **(
                        {"Referer": source.image_referer}
                        if source.image_referer
                        else {}
                    ),
                },
                max_bytes=context.manager.settings.image_proxy_max_bytes,
                signature_retry=True,
            )
            extension = await asyncio.to_thread(image_extension, result.part)
            final = target.with_suffix(extension)
            os.replace(result.part, final)
            await repo.save_file(
                task["id"],
                final.name,
                context.relative(final),
                size=result.size,
                downloaded_bytes=result.size,
                checksum=result.checksum,
                status="completed",
            )
            if final.name != target.name:
                await repo.remove_file_record(task["id"], target.name)
            paths[index] = final
            context.file_totals[index] = result.size
            context.done.add(index)
            await context.report(force=True)

    workers = [
        asyncio.create_task(page_worker())
        for _ in range(
            min(len(pending), context.manager.settings.download_image_concurrency)
        )
    ]
    try:
        await asyncio.gather(*workers)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    await context.checkpoint()
    destination = context.directory / (safe_filename(task["title"]) + ".cbz")
    # Shield the thread and join it on shutdown so cleanup cannot race an active ZIP writer.
    packing = asyncio.create_task(asyncio.to_thread(pack_cbz, destination, paths))
    try:
        await asyncio.shield(packing)
    except asyncio.CancelledError:
        await packing
        raise
    await context.checkpoint()
    return context.relative(destination)
