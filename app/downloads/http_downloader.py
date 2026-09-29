from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiofiles


class DownloadError(Exception):
    pass


class RetryableDownloadError(DownloadError):
    pass


def validate_url(url, allows_host):
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
        or not allows_host(parsed.hostname.lower())
    ):
        raise DownloadError("Download URL is outside the source allowlist")


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass
class DownloadResult:
    part: Path
    size: int
    checksum: str
    content_type: str


async def checked_await(awaitable, checkpoint):
    operation = asyncio.ensure_future(awaitable)
    try:
        while not operation.done():
            await asyncio.wait({operation}, timeout=0.25)
            if not operation.done():
                await checkpoint()
        return operation.result()
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def download_http(
    client,
    url,
    target: Path,
    *,
    allows_host,
    checkpoint,
    progress,
    headers=None,
    resume=True,
    max_bytes=None,
    signature_retry=False,
):
    """Stream through the source client, validating every redirect before following it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    offset = part.stat().st_size if resume and part.exists() else 0
    request_headers = {**(headers or {}), "Accept-Encoding": "identity"}
    if offset:
        request_headers["Range"] = f"bytes={offset}-"
    response = None
    try:
        for _ in range(11):
            await checkpoint()
            validate_url(url, allows_host)
            request = client.build_request("GET", url, headers=request_headers)
            response = await checked_await(
                client.send(request, stream=True, follow_redirects=False), checkpoint
            )
            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise DownloadError("Redirect has no destination")
                url = urljoin(url, location)
                continue
            break
        else:
            raise DownloadError("Too many download redirects")
        if response.status_code == 416 and offset:
            # A completed .part cannot be trusted without its expected size and validation.
            await response.aclose()
            part.unlink(missing_ok=True)
            return await download_http(
                client,
                url,
                target,
                allows_host=allows_host,
                checkpoint=checkpoint,
                progress=progress,
                headers=headers,
                resume=False,
                max_bytes=max_bytes,
                signature_retry=signature_retry,
            )
        if response.status_code >= 400:
            if (
                response.status_code in {408, 429}
                or response.status_code >= 500
                or (signature_retry and response.status_code in {401, 403})
            ):
                raise RetryableDownloadError(f"Upstream HTTP {response.status_code}")
            raise DownloadError(f"Upstream HTTP {response.status_code}")
        if response.status_code not in {200, 206}:
            raise DownloadError(f"Unexpected download response: {response.status_code}")
        total = None
        length = response.headers.get("content-length")
        if length is not None:
            if not length.isdigit():
                raise DownloadError("Invalid Content-Length")
            total = int(length)
        if response.status_code == 206:
            match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", "")
            )
            if not match or int(match[1]) != offset or int(match[2]) < offset:
                raise DownloadError("Invalid Content-Range for resumed download")
            if int(match[2]) != int(match[3]) - 1 or (
                total is not None and total != int(match[2]) - offset + 1
            ):
                raise DownloadError("Incomplete Content-Range")
            total = int(match[3])
        else:
            offset = 0
        if max_bytes is not None and (
            offset > max_bytes or (total is not None and total > max_bytes)
        ):
            raise DownloadError("Download exceeds the configured size limit")
        await progress(offset, total)
        async with aiofiles.open(part, "ab" if offset else "wb") as stream:
            chunks = response.aiter_bytes(64 * 1024).__aiter__()
            while True:
                try:
                    chunk = await checked_await(anext(chunks), checkpoint)
                except StopAsyncIteration:
                    break
                await checkpoint()
                offset += len(chunk)
                if max_bytes is not None and offset > max_bytes:
                    raise DownloadError("Download exceeds the configured size limit")
                await stream.write(chunk)
                await progress(offset, total)
        await checkpoint()
        if not offset:
            raise DownloadError("Downloaded file is empty")
        if total is not None and offset != total:
            raise RetryableDownloadError("Download ended before the expected length")
        return DownloadResult(
            part,
            offset,
            await asyncio.to_thread(checksum, part),
            response.headers.get("content-type", "").split(";")[0].lower(),
        )
    finally:
        if response is not None:
            await response.aclose()
