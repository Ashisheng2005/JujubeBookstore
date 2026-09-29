from __future__ import annotations

import asyncio
import os

import bencodepy

from ..resources import create_resource
from .filenames import safe_filename
from .http_downloader import DownloadError, checksum, download_http

# Download policies are kept separate from the existing resource parsing interface.
TORRENT_HOSTS = {"dmhy": ("share.dmhy.org", "dl.dmhy.org")}


def validate_torrent(path):
    content = path.read_bytes()
    try:
        value = bencodepy.decode(content)
        if not isinstance(value, dict) or not isinstance(value.get(b"info"), dict):
            raise ValueError("Missing torrent info dictionary")
        if bencodepy.encode(value) != content:
            raise ValueError("Malformed or trailing bencode data")
    except Exception as exc:
        raise DownloadError("Downloaded content is not a valid torrent") from exc


async def download_torrent(context):
    task = context.task
    source = create_resource(task["source"], context.manager.pool)
    url = task["payload"].get("torrent_url")
    if not url:
        detail = await source.detail(task["source_id"])
        url = detail.torrent
    if not url:
        raise DownloadError("Resource has no torrent file")
    hosts = TORRENT_HOSTS.get(task["source"], ())
    title = safe_filename(task["title"])
    name = title if title.lower().endswith(".torrent") else title + ".torrent"
    target = context.directory / name
    context.file_count = 1
    records = await context.manager.repo.files(task["id"])
    record = next((item for item in records if item["name"] == name), None)
    if (
        record
        and record["status"] == "completed"
        and target.is_file()
        and target.stat().st_size == record["size"]
        and record["checksum"] == await asyncio.to_thread(checksum, target)
    ):
        context.file_bytes[0] = record["size"]
        context.file_totals[0] = record["size"]
        context.done.add(0)
        await context.report(force=True)
        return context.relative(target)
    await context.manager.repo.save_file(task["id"], name, context.relative(target))

    async def progress(downloaded, total):
        context.file_bytes[0] = downloaded
        context.file_totals[0] = total
        await context.report()

    result = await download_http(
        source.client,
        url,
        target,
        allows_host=lambda host: host in hosts,
        checkpoint=context.checkpoint,
        progress=progress,
        headers=getattr(source, "site_headers", {}),
        resume=context.manager.settings.download_resume,
        max_bytes=32 * 1024 * 1024,
    )
    await asyncio.to_thread(validate_torrent, result.part)
    await context.checkpoint()
    os.replace(result.part, target)
    await context.manager.repo.save_file(
        task["id"],
        name,
        context.relative(target),
        size=result.size,
        downloaded_bytes=result.size,
        checksum=result.checksum,
        status="completed",
    )
    context.done.add(0)
    context.file_totals[0] = result.size
    await context.report(force=True)
    return context.relative(target)
