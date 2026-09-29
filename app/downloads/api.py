from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from ..errors import NotFoundError, ProxyRequiredError, SourceError
from ..resources import available_keys as resource_keys
from ..sources import available_keys, create_source
from .filenames import contained_path, safe_filename
from .http_downloader import DownloadError, validate_url
from .models import (
    BatchCreate,
    GroupCreate,
    GroupUpdate,
    TaskCreate,
    TaskStatus,
    TaskType,
)
from .repository import MissingRecord, StateConflict
from .torrent_downloader import TORRENT_HOSTS


async def download_service(request: Request):
    manager = getattr(request.app.state, "downloads", None)
    if manager is None:
        raise HTTPException(503, "Download manager has not started")
    try:
        yield manager
    except MissingRecord as exc:
        raise HTTPException(404, str(exc)) from exc
    except StateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ProxyRequiredError as exc:
        raise HTTPException(503, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc


router = APIRouter(prefix="/api", tags=["downloads"])


def validate_source(data):
    keys = available_keys() if data.type == "comic_chapter" else resource_keys()
    if data.source not in keys:
        raise HTTPException(404, "Unknown download source")
    if data.type == "torrent" and data.torrent_url:
        try:
            validate_url(
                data.torrent_url,
                lambda host: host in TORRENT_HOSTS.get(data.source, ()),
            )
        except (DownloadError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc


async def submit(manager, items, batch_id=None):
    for data in items:
        validate_source(data)
    tasks, created = await manager.repo.create_tasks(items, batch_id)
    for task in created:
        manager.events.publish("download.created", task)
        manager.enqueue(task["id"])
    return tasks, len(created)


async def batch_items(manager, data: BatchCreate):
    if data.source not in available_keys():
        raise HTTPException(404, "Unknown comic source")
    if data.group_id:
        async with manager.repo.lock:
            await manager.repo.group(data.group_id)
    detail = await create_source(data.source, manager.pool).detail(data.comic_id)
    chapters = [c for c in detail.chapters if c.group == data.chapter_group]
    if not chapters:
        raise HTTPException(404, "Chapter group is empty or does not exist")
    if len(chapters) > manager.settings.download_batch_max_items:
        raise HTTPException(
            422, f"Batch exceeds {manager.settings.download_batch_max_items} chapters"
        )
    return [
        TaskCreate(
            type="comic_chapter",
            source=data.source,
            source_id=f"comic-{data.comic_id}/chapter-{c.id}",
            comic_id=data.comic_id,
            chapter_id=c.id,
            title=f"{detail.title} {c.title}",
            group_id=data.group_id,
            output_format=data.output_format,
        )
        for c in chapters
    ]


@router.get("/download-groups")
async def list_groups(manager=Depends(download_service)):
    return await manager.repo.groups()


@router.post("/download-groups", status_code=201)
async def create_group(data: GroupCreate, manager=Depends(download_service)):
    if data.save_path:
        try:
            contained_path(manager.root, data.save_path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return await manager.repo.create_group(data)


@router.patch("/download-groups/{group_id}")
async def update_group(
    group_id: str, data: GroupUpdate, manager=Depends(download_service)
):
    if data.save_path:
        try:
            contained_path(manager.root, data.save_path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    result = await manager.repo.update_group(group_id, data)
    manager.events.publish("download.updated")
    return result


@router.delete("/download-groups/{group_id}", status_code=204)
async def delete_group(group_id: str, manager=Depends(download_service)):
    await manager.repo.delete_group(group_id)
    manager.events.publish("download.updated")
    return Response(status_code=204)


@router.get("/downloads/events")
async def download_events(request: Request, manager=Depends(download_service)):
    async def stream():
        async with manager.events.subscribe() as subscription:
            yield 'event: download.updated\ndata: {"type":"download.updated"}\n\n'
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(subscription.get(), timeout=15)
                    yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=True)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/downloads")
async def list_tasks(
    group_id: str | None = None,
    status: TaskStatus | None = None,
    type: TaskType | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    manager=Depends(download_service),
):
    return await manager.repo.tasks(
        group_id=group_id, status=status, type=type, page=page, page_size=page_size
    )


@router.post("/downloads", status_code=201)
async def create_task(data: TaskCreate, manager=Depends(download_service)):
    tasks, _ = await submit(manager, [data])
    return tasks[0]


@router.get("/download-batches/preview")
async def preview_batch(
    source: str = Query(min_length=1, max_length=100),
    comic_id: str = Query(min_length=1, max_length=500),
    chapter_group: str = Query(min_length=1, max_length=200),
    output_format: str = "cbz",
    group_id: str | None = None,
    manager=Depends(download_service),
):
    if output_format != "cbz":
        raise HTTPException(422, "Chapter batches only support cbz")
    data = BatchCreate(
        source=source, comic_id=comic_id, chapter_group=chapter_group, group_id=group_id
    )
    items = await batch_items(manager, data)
    existing = await manager.repo.existing_keys(
        [item.idempotency_key() for item in items]
    )
    return {
        "source": source,
        "comic_id": comic_id,
        "chapter_group": chapter_group,
        "count": len(items),
        "existing_count": len(existing),
        "new_count": len(items) - len(existing),
        "items": [
            {
                "chapter_id": item.chapter_id,
                "title": item.title,
                "exists": item.idempotency_key() in existing,
            }
            for item in items
        ],
    }


@router.post("/downloads/batch", status_code=201)
async def create_batch(data: BatchCreate, manager=Depends(download_service)):
    items = await batch_items(manager, data)
    batch_id = str(uuid4())
    tasks, created_count = await submit(manager, items, batch_id)
    return {
        "batch_id": batch_id,
        "task_ids": [task["id"] for task in tasks],
        "created_count": created_count,
        "existing_count": len(tasks) - created_count,
    }


@router.get("/download-batches/{batch_id}")
async def get_batch(batch_id: str, manager=Depends(download_service)):
    return await manager.repo.batch(batch_id)


@router.get("/downloads/{task_id}")
async def get_task(task_id: str, manager=Depends(download_service)):
    return await manager.repo.get_task(task_id)


@router.get("/downloads/{task_id}/children")
async def task_children(task_id: str, manager=Depends(download_service)):
    await manager.repo.get_task(task_id)
    return []


@router.get("/downloads/{task_id}/files")
async def get_files(task_id: str, manager=Depends(download_service)):
    await manager.repo.get_task(task_id)
    return await manager.repo.files(task_id)


@router.get("/downloads/{task_id}/content")
async def task_content(task_id: str, manager=Depends(download_service)):
    task = await manager.repo.get_task(task_id)
    if task["status"] != "completed" or task["cleanup_state"] != "none":
        raise HTTPException(409, "Download is not completed or is being deleted")
    if not task["destination"]:
        raise HTTPException(404, "Completed download has no file")
    try:
        path = contained_path(manager.root, task["destination"])
    except ValueError as exc:
        raise HTTPException(404, "Invalid download destination") from exc
    if not path.is_file():
        raise HTTPException(404, "Downloaded file no longer exists")
    extension = ".cbz" if task["type"] == "comic_chapter" else ".torrent"
    name = safe_filename(task["title"])
    if not name.lower().endswith(extension):
        name += extension
    return FileResponse(
        path,
        filename=name,
        media_type="application/vnd.comicbook+zip"
        if extension == ".cbz"
        else "application/x-bittorrent",
    )


@router.delete("/downloads/{task_id}", status_code=204)
async def delete_task(task_id: str, manager=Depends(download_service)):
    try:
        await manager.delete(task_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            500, "File cleanup failed; task is retained for startup cleanup"
        ) from exc
    return Response(status_code=204)


@router.post("/downloads/{task_id}/pause")
async def pause_task(task_id: str, manager=Depends(download_service)):
    return await manager.control(task_id, "pause")


@router.post("/downloads/{task_id}/resume")
async def resume_task(task_id: str, manager=Depends(download_service)):
    return await manager.control(task_id, "resume")


@router.post("/downloads/{task_id}/cancel")
async def cancel_task(task_id: str, manager=Depends(download_service)):
    return await manager.control(task_id, "cancel")


@router.post("/downloads/{task_id}/retry")
async def retry_task(task_id: str, manager=Depends(download_service)):
    return await manager.control(task_id, "retry")
