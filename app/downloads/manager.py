from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

import httpx

from ..errors import NotFoundError, ProxyRequiredError
from .comic_downloader import download_comic
from .events import EventBus
from .filenames import contained_path
from .http_downloader import RetryableDownloadError
from .repository import MissingRecord, now
from .torrent_downloader import download_torrent

logger = logging.getLogger(__name__)


class TaskInterrupted(Exception):
    pass


def is_retryable(exc):
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, (NotFoundError, ProxyRequiredError, OSError)):
        return False
    if isinstance(exc, RetryableDownloadError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429} or exc.response.status_code >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    return is_retryable(exc.__cause__) if exc.__cause__ is not None else False


class DownloadContext:
    def __init__(self, manager, task):
        self.manager, self.task = manager, task
        self.directory = manager.task_directory(task)
        self.file_bytes = {}
        self.file_totals = {}
        self.done = set()
        self.file_count = 0
        self.signal = asyncio.Event()
        self.last_check = 0
        self.last_report = time.monotonic()
        self.last_bytes = 0
        self.has_reported = False
        self.report_lock = asyncio.Lock()

    def relative(self, path):
        value = path.relative_to(self.manager.root).as_posix()
        contained_path(self.manager.root, value)
        return value

    async def checkpoint(self):
        current = time.monotonic()
        if self.signal.is_set() or current - self.last_check >= 0.25:
            task = await self.manager.repo.get_task(self.task["id"])
            self.last_check = current
            if task["requested_action"] != "none" or self.manager.stopping:
                raise TaskInterrupted()

    async def report(self, force=False):
        async with self.report_lock:
            current = time.monotonic()
            downloaded = sum(self.file_bytes.values())
            elapsed = current - self.last_report
            if (
                not force
                and elapsed < 0.25
                and abs(downloaded - self.last_bytes) < 256 * 1024
            ):
                return
            total = (
                sum(self.file_totals.values())
                if len(self.file_totals) == self.file_count
                and all(value is not None for value in self.file_totals.values())
                else None
            )
            speed = (
                max(0, downloaded - self.last_bytes) / max(elapsed, 0.001)
                if self.has_reported
                else 0
            )
            partial = sum(
                min(0.99, self.file_bytes.get(index, 0) / size)
                for index, size in self.file_totals.items()
                if size and index not in self.done
            )
            progress = 99 * (len(self.done) + partial) / max(self.file_count, 1)
            eta = (
                max(0, int((total - downloaded) / speed))
                if total is not None and speed
                else None
            )
            await self.manager.repo.update(
                self.task["id"],
                {
                    "downloaded_bytes": downloaded,
                    "total_bytes": total,
                    "completed_files": len(self.done),
                    "file_count": self.file_count,
                    "progress": min(99, progress),
                    "speed": speed,
                    "eta": eta,
                },
                statuses=("downloading",),
                action="none",
            )
            self.last_report, self.last_bytes = current, downloaded
            self.has_reported = True
            await self.manager.publish("download.updated", self.task["id"])

    async def record_partials(self):
        if not self.directory.exists():
            return
        records = {
            record["path"]: record
            for record in await self.manager.repo.files(self.task["id"])
        }
        for part in self.directory.rglob("*.part"):
            path = part.with_name(part.name.removesuffix(".part"))
            relative = self.relative(path)
            if records.get(relative, {}).get("status") != "completed":
                await self.manager.repo.save_file(
                    self.task["id"],
                    path.name,
                    relative,
                    downloaded_bytes=part.stat().st_size,
                )


class DownloadManager:
    retry_delays = (2, 5, 15)

    def __init__(self, repo, pool, settings):
        self.repo, self.pool, self.settings = repo, pool, settings
        self.root = Path(settings.download_dir).resolve()
        self.events = EventBus()
        self.queue = asyncio.Queue()
        self.enqueued = set()
        self.task_controls = {}
        self.workers = []
        self.retries = set()
        self.deletion_lock = asyncio.Lock()
        self.stopping = False

    def task_directory(self, task):
        prefix = task["payload"].get("save_path")
        relative = f"{prefix}/{task['id']}" if prefix else task["id"]
        return contained_path(self.root, relative)

    async def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        tasks = await self.repo.recovery()
        for task in tasks:
            if task["cleanup_state"] == "pending":
                try:
                    await self.cleanup(task)
                except (OSError, ValueError):
                    logger.exception(
                        "Could not finish cleanup of download %s", task["id"]
                    )
                continue
            try:
                directory = self.task_directory(task)
                if directory.exists():
                    for temporary in directory.glob("*.cbz.tmp"):
                        contained_path(
                            self.root, temporary.relative_to(self.root).as_posix()
                        ).unlink()
                    known = {
                        record["path"] for record in await self.repo.files(task["id"])
                    }
                    for part in directory.rglob("*.part"):
                        path = part.with_name(part.name.removesuffix(".part"))
                        relative = path.relative_to(self.root).as_posix()
                        contained_path(self.root, relative)
                        if relative not in known:
                            await self.repo.save_file(
                                task["id"],
                                path.name,
                                relative,
                                downloaded_bytes=part.stat().st_size,
                            )
            except (OSError, ValueError):
                logger.exception("Could not inspect download %s", task["id"])
            if task["status"] == "queued":
                self.enqueue(task["id"])
        self.workers = [
            asyncio.create_task(self.worker_loop(), name=f"download-worker-{i}")
            for i in range(self.settings.download_workers)
        ]
        self.events.publish("download.updated")

    async def stop(self):
        self.stopping = True
        pending = [*self.workers, *self.retries]
        for worker in pending:
            worker.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.workers.clear()
        self.retries.clear()

    def enqueue(self, task_id):
        if task_id not in self.enqueued and not self.stopping:
            self.enqueued.add(task_id)
            self.queue.put_nowait(task_id)

    async def publish(self, event_type, task_id):
        try:
            self.events.publish(event_type, await self.repo.get_task(task_id))
        except MissingRecord:
            pass

    async def control(self, task_id, action):
        task = await self.repo.control(task_id, action)
        context = self.task_controls.get(task_id)
        if context and task["requested_action"] != "none":
            context.signal.set()
        if task["status"] == "queued":
            self.enqueue(task_id)
        self.events.publish("download.updated", task)
        return task

    async def confirm_action(self, task_id):
        while True:
            task = await self.repo.get_task(task_id)
            action = task["requested_action"]
            if action not in {"pause", "cancel"} or task["status"] != "downloading":
                return False
            changed = await self.repo.update(
                task_id,
                {
                    "status": "paused" if action == "pause" else "canceled",
                    "requested_action": "none",
                    "speed": 0,
                    "eta": None,
                },
                statuses=("downloading",),
                action=action,
            )
            if changed:
                await self.publish("download.updated", task_id)
                return True

    async def record_partials(self, context):
        if context:
            try:
                await context.record_partials()
            except (OSError, ValueError):
                logger.exception(
                    "Could not record partial files for %s", context.task["id"]
                )

    async def worker_loop(self):
        while True:
            task_id = await self.queue.get()
            self.enqueued.discard(task_id)
            try:
                await self.execute(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Download worker error for %s", task_id)
            finally:
                self.queue.task_done()

    async def execute(self, task_id):
        try:
            task = await self.repo.get_task(task_id)
        except MissingRecord:
            return
        if task["cleanup_state"] != "none":
            return
        if not await self.repo.update(
            task_id,
            {"status": "downloading", "started_at": task["started_at"] or now()},
            statuses=("queued",),
            action="none",
        ):
            return
        context = None
        try:
            task = await self.repo.get_task(task_id)
            context = DownloadContext(self, task)
            self.task_controls[task_id] = context
            await context.checkpoint()
            await self.publish("download.updated", task_id)
            handler = (
                download_comic if task["type"] == "comic_chapter" else download_torrent
            )
            destination = await handler(context)
            if await self.repo.update(
                task_id,
                {
                    "status": "completed",
                    "destination": destination,
                    "completed_at": now(),
                    "progress": 100,
                    "downloaded_bytes": sum(context.file_bytes.values()),
                    "total_bytes": sum(context.file_bytes.values()),
                    "completed_files": context.file_count,
                    "speed": 0,
                    "eta": 0,
                    "error_message": None,
                },
                statuses=("downloading",),
                action="none",
            ):
                await self.publish("download.completed", task_id)
            else:
                await self.confirm_action(task_id)
        except TaskInterrupted:
            await self.record_partials(context)
            await self.confirm_action(task_id)
        except asyncio.CancelledError:
            await self.record_partials(context)
            await self.confirm_action(task_id)
            raise
        except Exception as exc:
            await self.record_partials(context)
            if await self.confirm_action(task_id):
                return
            latest = await self.repo.get_task(task_id)
            retries = latest["retry_count"]
            if is_retryable(exc) and retries < self.settings.download_retries:
                if await self.repo.update(
                    task_id,
                    {
                        "status": "queued",
                        "retry_count": retries + 1,
                        "error_message": str(exc),
                        "speed": 0,
                        "eta": None,
                    },
                    statuses=("downloading",),
                    action="none",
                ):
                    await self.publish("download.updated", task_id)
                    delay = self.retry_delays[min(retries, len(self.retry_delays) - 1)]
                    retry = asyncio.create_task(self.delayed_enqueue(task_id, delay))
                    self.retries.add(retry)
                    retry.add_done_callback(self.retries.discard)
                else:
                    await self.confirm_action(task_id)
            elif await self.repo.update(
                task_id,
                {
                    "status": "failed",
                    "error_message": str(exc) or type(exc).__name__,
                    "speed": 0,
                    "eta": None,
                },
                statuses=("downloading",),
                action="none",
            ):
                await self.publish("download.failed", task_id)
            else:
                await self.confirm_action(task_id)
        finally:
            self.task_controls.pop(task_id, None)

    async def delayed_enqueue(self, task_id, delay):
        await asyncio.sleep(delay)
        self.enqueue(task_id)

    async def cleanup(self, task):
        directory = self.task_directory(task)
        if directory.exists():
            await asyncio.to_thread(shutil.rmtree, directory)
        await self.repo.delete_task(task["id"])
        self.events.publish("download.deleted", {"id": task["id"]})

    async def delete(self, task_id):
        async with self.deletion_lock:
            task = await self.repo.mark_cleanup(task_id)
            await self.cleanup(task)
