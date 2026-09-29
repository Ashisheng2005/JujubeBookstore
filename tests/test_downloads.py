from __future__ import annotations

import asyncio
import base64
import json
import threading
import zipfile
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import load_settings
from app.downloads.events import EventBus
from app.downloads.filenames import contained_path, relative_path, safe_filename
from app.downloads.http_downloader import DownloadError, download_http
from app.downloads.manager import DownloadContext, DownloadManager, is_retryable
from app.downloads.models import GroupCreate, GroupUpdate, TaskCreate
from app.downloads.repository import DownloadRepository, StateConflict
from app.downloads.torrent_downloader import TORRENT_HOSTS, validate_torrent
from app.errors import NotFoundError, ProxyRequiredError
from app.main import app
from app.resources import register as register_resource
from app.resources.base import ResourceSource
from app.schemas import ChapterImages, ChapterInfo, ComicDetail, ResourceDetail
from app.sources import register
from app.sources.base import ComicSource

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZfkAAAAASUVORK5CYII="
)
TORRENT = b"d4:infod4:name4:testee"


def chapter_task(chapter="1", **kwargs):
    return TaskCreate(
        type="comic_chapter",
        source="downloadfake",
        source_id=f"comic-1/chapter-{chapter}",
        comic_id="1",
        chapter_id=chapter,
        title=f"Chapter {chapter}",
        **kwargs,
    )


@register
class DownloadFake(ComicSource):
    key = "downloadfake"
    name = "Download test source"
    image_hosts = ("images.test", "127.0.0.1")
    image_referer = "https://images.test/"
    image_base = "https://images.test"
    calls = 0

    async def search(self, keyword, page=1):
        return []

    async def detail(self, comic_id):
        type(self).calls += 1
        return ComicDetail(
            id=comic_id,
            title="Test comic",
            chapters=[
                ChapterInfo(id=str(i), title=f"Chapter {i}", group="serial")
                for i in range(1, 4)
            ],
        )

    async def chapter(self, comic_id, chapter_id):
        return ChapterImages(
            source=self.key,
            comic_id=comic_id,
            chapter_id=chapter_id,
            count=2,
            images=[self.image_base + f"/{i}.jpg" for i in range(2)],
        )


@register_resource
class DownloadResourceFake(ResourceSource):
    key = "downloadresourcefake"
    name = "Download resource test source"

    async def search(self, keyword, page=1, category=None):
        return []

    async def detail(self, item_id):
        return ResourceDetail(
            id=item_id,
            title="Test torrent",
            torrent="https://torrent.test/file.torrent",
        )


class FakePool:
    def __init__(self, client):
        self.http = client
        self.proxy_available = False

    def client(self, **kwargs):
        return self.http


async def repository_case(tmp_path, callback):
    repo = DownloadRepository(tmp_path / "downloads.db")
    await repo.open()
    try:
        await callback(repo)
    finally:
        await repo.close()


@pytest.mark.parametrize(
    "value",
    [
        "../escape",
        "/absolute",
        "C:\\escape",
        "C:escape",
        "\\\\host\\share",
        "a/../b",
        "a//b",
        "a/CON",
        "a/name.",
        "a:x",
        ".",
    ],
)
def test_relative_path_rejects_escapes(value):
    with pytest.raises(ValueError):
        relative_path(value)


def test_filenames_and_containment(tmp_path):
    assert safe_filename("../CON:a*?") == "_CON_a__"
    assert safe_filename("CON.txt") == "_CON.txt"
    assert safe_filename("..") == "download"
    assert relative_path("comic\\chapter") == "comic/chapter"
    assert contained_path(tmp_path, "comic/chapter") == tmp_path / "comic" / "chapter"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"output_format": "file"},
        {"output_format": "directory"},
        {"torrent_url": "https://evil.test/"},
    ],
)
def test_invalid_task_formats(kwargs):
    with pytest.raises(ValidationError):
        chapter_task(**kwargs)


def test_repository_crud_idempotency_batch_and_group_path(tmp_path):
    async def check(repo):
        group = await repo.create_group(GroupCreate(name="Group", save_path="comics"))
        first, created = await repo.create_tasks([chapter_task(group_id=group["id"])])
        duplicate, extra = await repo.create_tasks([chapter_task()])
        assert duplicate[0]["id"] == first[0]["id"] and not extra and len(created) == 1
        assert first[0]["payload"]["save_path"] == "comics"
        await repo.update_group(group["id"], GroupUpdate(save_path="other"))
        assert (await repo.get_task(first[0]["id"]))["payload"]["save_path"] == "comics"
        for batch in ("batch-a", "batch-b"):
            tasks, _ = await repo.create_tasks(
                [chapter_task(), chapter_task("2")], batch
            )
            assert (await repo.batch(batch))["total"] == 2
        assert (await repo.batch("batch-a"))["total"] == 2
        await repo.delete_group(group["id"])
        assert (await repo.get_task(first[0]["id"]))["group_id"] is None
        await repo.control(first[0]["id"], "cancel")
        reused, created = await repo.create_tasks([chapter_task()])
        assert reused[0]["id"] == first[0]["id"] and reused[0]["status"] == "queued"
        assert len(created) == 1
        assert (await repo.tasks(type="comic_chapter", page_size=1))["total"] == 2
        assert (await repo.tasks(group_id="ungrouped"))["total"] == 2

    asyncio.run(repository_case(tmp_path, check))


def test_repository_transaction_rolls_back_invalid_group(tmp_path):
    async def check(repo):
        with pytest.raises(Exception, match="group not found"):
            await repo.create_tasks(
                [chapter_task(), chapter_task("2", group_id="missing")], "batch"
            )
        assert (await repo.tasks())["total"] == 0
        async with repo.lock:
            assert not await repo._rows("SELECT * FROM download_batches")

    asyncio.run(repository_case(tmp_path, check))


def test_conditional_claim_and_pause_cancel_race(tmp_path):
    async def check(repo):
        tasks, _ = await repo.create_tasks([chapter_task()])
        task_id = tasks[0]["id"]
        claims = await asyncio.gather(
            *(
                repo.update(
                    task_id,
                    {"status": "downloading"},
                    statuses=("queued",),
                    action="none",
                )
                for _ in range(2)
            )
        )
        assert sorted(claims) == [False, True]
        assert (await repo.control(task_id, "pause"))["status"] == "downloading"
        assert not await repo.update(
            task_id, {"status": "completed"}, statuses=("downloading",), action="none"
        )
        await repo.control(task_id, "cancel")
        assert not await repo.update(
            task_id, {"status": "paused"}, statuses=("downloading",), action="pause"
        )
        assert await repo.update(
            task_id,
            {"status": "canceled", "requested_action": "none"},
            statuses=("downloading",),
            action="cancel",
        )
        assert (await repo.control(task_id, "retry"))["status"] == "queued"
        with pytest.raises(StateConflict):
            await repo.control(task_id, "resume")

    asyncio.run(repository_case(tmp_path, check))


def test_restart_preserves_pause_and_retry_count(tmp_path):
    async def check(repo):
        tasks, _ = await repo.create_tasks([chapter_task(str(i)) for i in range(3)])
        await repo.update(
            tasks[0]["id"],
            {"status": "downloading", "requested_action": "pause", "retry_count": 2},
        )
        await repo.update(tasks[1]["id"], {"status": "pending"})
        await repo.control(tasks[2]["id"], "pause")
        await repo.close()
        await repo.open()
        recovered = {t["id"]: t for t in await repo.recovery()}
        assert recovered[tasks[0]["id"]]["status"] == "queued"
        assert recovered[tasks[0]["id"]]["requested_action"] == "none"
        assert recovered[tasks[0]["id"]]["retry_count"] == 2
        assert recovered[tasks[1]["id"]]["status"] == "queued"
        assert recovered[tasks[2]["id"]]["status"] == "paused"

    asyncio.run(repository_case(tmp_path, check))


async def noop(*args):
    pass


def test_http_range_resume_and_ignored_range(tmp_path):
    async def check():
        target = tmp_path / "test.torrent"
        part = target.with_suffix(".torrent.part")
        seen = []

        def handler(request):
            seen.append(request.headers.get("range"))
            assert request.headers["referer"] == "https://torrent.test/"
            if len(seen) == 1:
                return httpx.Response(
                    206,
                    content=TORRENT[5:],
                    headers={
                        "Content-Range": f"bytes 5-{len(TORRENT) - 1}/{len(TORRENT)}"
                    },
                )
            return httpx.Response(200, content=TORRENT)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(2):
                part.write_bytes(TORRENT[:5])
                result = await download_http(
                    client,
                    "https://torrent.test/a",
                    target,
                    allows_host=lambda h: h == "torrent.test",
                    checkpoint=noop,
                    progress=noop,
                    headers={"Referer": "https://torrent.test/"},
                )
                assert result.part.read_bytes() == TORRENT
        assert seen == ["bytes=5-", "bytes=5-"]

    asyncio.run(check())


def test_http_416_restarts_and_rejects_invalid_range(tmp_path):
    async def check():
        target = tmp_path / "file"
        target.with_name("file.part").write_bytes(b"too long")
        ranges = []

        def handler(request):
            ranges.append(request.headers.get("range"))
            return (
                httpx.Response(416)
                if ranges[-1]
                else httpx.Response(200, content=TORRENT)
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await download_http(
                client,
                "https://torrent.test/a",
                target,
                allows_host=lambda h: True,
                checkpoint=noop,
                progress=noop,
            )
            assert result.part.read_bytes() == TORRENT and ranges == ["bytes=8-", None]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    206, content=b"x", headers={"Content-Range": "bytes 0-0/1"}
                )
            )
        ) as client:
            with pytest.raises(DownloadError, match="Content-Range"):
                await download_http(
                    client,
                    "https://torrent.test/a",
                    target,
                    allows_host=lambda h: True,
                    checkpoint=noop,
                    progress=noop,
                )

    asyncio.run(check())


def test_download_rejects_redirect_before_request(tmp_path):
    async def check():
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DownloadError, match="allowlist"):
                await download_http(
                    client,
                    "https://torrent.test/a",
                    tmp_path / "file",
                    allows_host=lambda h: h == "torrent.test",
                    checkpoint=noop,
                    progress=noop,
                )
        assert seen == ["https://torrent.test/a"]

    asyncio.run(check())


def test_sse_coalesces_progress_and_unsubscribes():
    async def check():
        bus = EventBus(queue_limit=2)
        async with bus.subscribe() as subscriber:
            for progress in range(100):
                bus.publish(
                    "download.updated",
                    {"id": "one", "type": "comic_chapter", "progress": progress},
                )
            assert len(subscriber.pending) == 1
            latest = await subscriber.get()
            assert latest["progress"] == 99 and latest["type"] == "download.updated"
            assert latest["task_type"] == "comic_chapter"
            for task in ("one", "two", "three"):
                bus.publish("download.completed", {"id": task})
            assert len(subscriber.pending) == 2
            assert (await subscriber.get())["type"] == "download.completed"
        assert not bus.subscribers

    asyncio.run(check())


@pytest.mark.parametrize(
    "content", [b"", b"<html>error</html>", b"d4:infodeeTRAIL", b"d8:announce1:xe"]
)
def test_torrent_validation_rejects_invalid_data(tmp_path, content):
    file = tmp_path / "file"
    file.write_bytes(content)
    with pytest.raises(DownloadError):
        validate_torrent(file)


def test_torrent_validation_accepts_no_announce(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(TORRENT)
    validate_torrent(file)


@pytest.fixture()
def download_api(tmp_path, monkeypatch):
    monkeypatch.setenv("JUJUBE_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.delenv("JUJUBE_DOWNLOAD_DB", raising=False)

    async def idle(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(DownloadManager, "worker_loop", idle)
    with TestClient(app) as client:
        yield client


def test_download_api_groups_tasks_controls_and_content(download_api, tmp_path):
    client = download_api
    group = client.post(
        "/api/download-groups", json={"name": "Group", "save_path": "comics"}
    ).json()
    renamed = client.patch(
        "/api/download-groups/" + group["id"], json={"name": "Renamed"}
    ).json()
    assert renamed["name"] == "Renamed" and renamed["save_path"] == "comics"
    assert (
        client.post(
            "/api/download-groups", json={"name": "bad", "save_path": "../escape"}
        ).status_code
        == 422
    )
    task = client.post(
        "/api/downloads", json=chapter_task(group_id=group["id"]).model_dump()
    ).json()
    task_id = task["id"]
    assert (
        client.post("/api/downloads", json=chapter_task().model_dump()).json()["id"]
        == task_id
    )
    assert (
        client.get(
            "/api/downloads", params={"group_id": group["id"], "status": "queued"}
        ).json()["total"]
        == 1
    )
    assert client.get(f"/api/downloads/{task_id}/content").status_code == 409
    assert client.delete(f"/api/downloads/{task_id}").status_code == 409
    for action, status in [
        ("pause", "paused"),
        ("resume", "queued"),
        ("cancel", "canceled"),
        ("retry", "queued"),
    ]:
        assert (
            client.post(f"/api/downloads/{task_id}/{action}").json()["status"] == status
        )
    assert client.post(f"/api/downloads/{task_id}/resume").status_code == 409
    directory = app.state.downloads.task_directory(task)
    directory.mkdir(parents=True)
    artifact = directory / "test.cbz"
    artifact.write_bytes(b"completed-test-file")
    client.portal.call(
        app.state.downloads.repo.update,
        task_id,
        {
            "status": "completed",
            "destination": artifact.relative_to(tmp_path).as_posix(),
        },
    )
    assert (
        client.get(f"/api/downloads/{task_id}/content").content
        == b"completed-test-file"
    )
    ranged = client.get(
        f"/api/downloads/{task_id}/content", headers={"Range": "bytes=0-3"}
    )
    assert ranged.status_code == 206 and ranged.content == b"comp"
    assert client.delete("/api/download-groups/" + group["id"]).status_code == 204
    assert (
        artifact.exists()
        and client.get(f"/api/downloads/{task_id}").json()["group_id"] is None
    )
    assert client.get(f"/api/downloads/{task_id}/children").json() == []
    assert client.delete(f"/api/downloads/{task_id}").status_code == 204
    assert (
        not directory.exists()
        and client.get(f"/api/downloads/{task_id}").status_code == 404
    )


def test_batch_api_preview_limit_and_reused_membership(download_api, monkeypatch):
    client = download_api
    DownloadFake.calls = 0
    params = {"source": "downloadfake", "comic_id": "1", "chapter_group": "serial"}
    preview = client.get("/api/download-batches/preview", params=params)
    assert preview.json()["count"] == 3 and preview.json()["existing_count"] == 0
    assert client.get("/api/downloads").json()["total"] == 0
    DownloadFake.calls = 0
    batch = client.post("/api/downloads/batch", json=params).json()
    assert DownloadFake.calls == 1 and batch["created_count"] == 3
    assert (
        client.get("/api/download-batches/" + batch["batch_id"]).json()["counts"][
            "queued"
        ]
        == 3
    )
    assert (
        client.get("/api/download-batches/preview", params=params).json()[
            "existing_count"
        ]
        == 3
    )
    reused = client.post("/api/downloads/batch", json=params).json()
    assert reused["created_count"] == 0 and set(reused["task_ids"]) == set(
        batch["task_ids"]
    )
    assert client.get("/api/download-batches/" + batch["batch_id"]).json()["total"] == 3
    app.state.downloads.settings = replace(
        app.state.downloads.settings, download_batch_max_items=2
    )
    assert client.post("/api/downloads/batch", json=params).status_code == 422
    assert client.get("/api/downloads").json()["total"] == 3


def test_download_api_validation(download_api):
    client = download_api
    assert (
        client.post(
            "/api/downloads", json={**chapter_task().model_dump(), "source": "missing"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/downloads", json=chapter_task(group_id="missing").model_dump()
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/downloads",
            json={
                "type": "torrent",
                "source": "dmhy",
                "source_id": "1",
                "title": "test",
                "torrent_url": "http://127.0.0.1/private",
            },
        ).status_code
        == 422
    )
    assert client.get("/api/downloads", params={"page": 0}).status_code == 422
    assert (
        client.get(
            "/api/download-batches/preview",
            params={"source": "downloadfake", "comic_id": "1", "chapter_group": ""},
        ).status_code
        == 422
    )


async def wait_status(repo, task_id, status):
    async with asyncio.timeout(5):
        while True:
            task = await repo.get_task(task_id)
            if task["status"] == status:
                return task
            if task["status"] == "failed" and status != "failed":
                raise AssertionError(task["error_message"])
            await asyncio.sleep(0.01)


def test_comic_worker_packages_images_and_refreshes_expired_urls(tmp_path, monkeypatch):
    async def check(repo):
        attempts = []
        chapters = []

        async def chapter(self, comic_id, chapter_id):
            chapters.append(1)
            return ChapterImages(
                source=self.key,
                comic_id=comic_id,
                chapter_id=chapter_id,
                count=2,
                images=[
                    f"https://images.test/{i}.jpg?attempt={len(chapters)}"
                    for i in range(2)
                ],
            )

        monkeypatch.setattr(DownloadFake, "chapter", chapter)

        def handler(request):
            attempts.append(str(request.url))
            assert request.headers["referer"] == "https://images.test/"
            return (
                httpx.Response(403)
                if request.url.params["attempt"] == "1"
                else httpx.Response(
                    200, content=PNG, headers={"Content-Type": "image/png"}
                )
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            manager = DownloadManager(
                repo,
                FakePool(http),
                replace(load_settings(), download_dir=str(tmp_path)),
            )
            manager.retry_delays = (0, 0, 0)
            tasks, _ = await repo.create_tasks([chapter_task()])
            await manager.start()
            try:
                task = await wait_status(repo, tasks[0]["id"], "completed")
                assert (
                    len(chapters) == 2
                    and task["retry_count"] == 1
                    and task["progress"] == 100
                )
                assert (
                    task["downloaded_bytes"] == 2 * len(PNG)
                    and task["completed_files"] == 2
                )
                with zipfile.ZipFile(tmp_path / task["destination"]) as cbz:
                    assert cbz.namelist() == ["001.png", "002.png"]
                    assert cbz.read("001.png") == PNG
                assert "?" not in json.dumps(await repo.files(task["id"]))
            finally:
                await manager.stop()

    asyncio.run(repository_case(tmp_path, check))


def test_retry_limit_and_nonretryable_error_classification(tmp_path):
    assert not is_retryable(NotFoundError("missing"))
    assert not is_retryable(ProxyRequiredError("proxy"))
    assert not is_retryable(PermissionError("disk"))
    assert is_retryable(httpx.ReadTimeout("timeout"))
    assert is_retryable(ConnectionResetError("reset"))

    async def check(repo):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            settings = replace(
                load_settings(),
                download_dir=str(tmp_path),
                download_retries=2,
                download_image_concurrency=1,
            )
            manager = DownloadManager(repo, FakePool(http), settings)
            manager.retry_delays = (0, 0, 0)
            tasks, _ = await repo.create_tasks([chapter_task()])
            await manager.start()
            try:
                task = await wait_status(repo, tasks[0]["id"], "failed")
                assert task["retry_count"] == 2 and len(attempts) == 3
            finally:
                await manager.stop()

    asyncio.run(repository_case(tmp_path, check))


def test_torrent_worker_uses_source_client_and_resume(tmp_path, monkeypatch):
    monkeypatch.setitem(TORRENT_HOSTS, "downloadresourcefake", ("torrent.test",))

    async def check(repo):
        tasks, _ = await repo.create_tasks(
            [
                TaskCreate(
                    type="torrent",
                    source="downloadresourcefake",
                    source_id="item",
                    title="test",
                )
            ]
        )
        task_id = tasks[0]["id"]
        directory = tmp_path / task_id
        directory.mkdir()
        (directory / "test.torrent.part").write_bytes(TORRENT[:5])

        def handler(request):
            assert request.headers["range"] == "bytes=5-"
            return httpx.Response(
                206,
                content=TORRENT[5:],
                headers={"Content-Range": f"bytes 5-{len(TORRENT) - 1}/{len(TORRENT)}"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            manager = DownloadManager(
                repo,
                FakePool(http),
                replace(load_settings(), download_dir=str(tmp_path)),
            )
            await manager.start()
            try:
                task = await wait_status(repo, task_id, "completed")
                assert (tmp_path / task["destination"]).read_bytes() == TORRENT
                assert (await repo.files(task_id))[0]["status"] == "completed"
            finally:
                await manager.stop()

    asyncio.run(repository_case(tmp_path, check))


def test_comic_restart_keeps_completed_pages_with_local_http_server(
    tmp_path, monkeypatch
):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(PNG)))
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(PNG)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        DownloadFake, "image_base", f"http://127.0.0.1:{server.server_port}"
    )
    # Local integration uses an explicitly registered test policy for the ephemeral port.
    import app.downloads.http_downloader as http_module

    original_validate = http_module.validate_url

    def validate(url, allows_host):
        if url.startswith(f"http://127.0.0.1:{server.server_port}/"):
            return
        original_validate(url, allows_host)

    monkeypatch.setattr(http_module, "validate_url", validate)

    async def check(repo):
        async with httpx.AsyncClient(trust_env=False) as http:
            settings = replace(
                load_settings(),
                download_dir=str(tmp_path),
                download_image_concurrency=1,
            )
            manager = DownloadManager(repo, FakePool(http), settings)
            tasks, _ = await repo.create_tasks([chapter_task()])
            task_id = tasks[0]["id"]
            await manager.start()
            try:
                task = await wait_status(repo, task_id, "completed")
            finally:
                await manager.stop()
            assert seen == ["/0.jpg", "/1.jpg"]
            await repo.update(
                task_id, {"status": "downloading", "requested_action": "none"}
            )
            second = DownloadManager(repo, FakePool(http), settings)
            await second.start()
            try:
                recovered = await wait_status(repo, task_id, "completed")
                assert (
                    recovered["destination"] == task["destination"] and len(seen) == 2
                )
            finally:
                await second.stop()

    try:
        asyncio.run(repository_case(tmp_path, check))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cleanup_failure_is_persistent_and_recovered(tmp_path, monkeypatch):
    async def check(repo):
        tasks, _ = await repo.create_tasks([chapter_task()])
        task_id = tasks[0]["id"]
        directory = tmp_path / task_id
        directory.mkdir()
        (directory / "file.part").write_bytes(b"partial")
        await repo.control(task_id, "cancel")
        manager = DownloadManager(
            repo, None, replace(load_settings(), download_dir=str(tmp_path))
        )
        import app.downloads.manager as manager_module

        real = manager_module.shutil.rmtree

        def fail(*args):
            raise PermissionError("locked")

        monkeypatch.setattr(manager_module.shutil, "rmtree", fail)
        with pytest.raises(PermissionError):
            await manager.delete(task_id)
        assert (await repo.get_task(task_id))[
            "cleanup_state"
        ] == "pending" and directory.exists()
        with pytest.raises(StateConflict):
            await manager.control(task_id, "retry")
        monkeypatch.setattr(manager_module.shutil, "rmtree", real)
        await manager.start()
        await manager.stop()
        assert not directory.exists() and (await repo.tasks())["total"] == 0

    asyncio.run(repository_case(tmp_path, check))


def test_progress_speed_eta_ignores_resumed_bytes(tmp_path):
    async def check(repo):
        tasks, _ = await repo.create_tasks([chapter_task()])
        task = tasks[0]
        await repo.update(task["id"], {"status": "downloading"})
        manager = DownloadManager(
            repo, None, replace(load_settings(), download_dir=str(tmp_path))
        )
        context = DownloadContext(manager, task)
        context.file_count = 1
        context.file_bytes[0] = 100
        context.file_totals[0] = 1000
        await context.report(force=True)
        first = await repo.get_task(task["id"])
        assert first["speed"] == 0 and first["eta"] is None
        context.last_report -= 1
        context.file_bytes[0] = 200
        await context.report(force=True)
        second = await repo.get_task(task["id"])
        assert 90 < second["speed"] < 110 and second["eta"] in {8, 9}
        assert second["progress"] == pytest.approx(19.8)

    asyncio.run(repository_case(tmp_path, check))


@pytest.mark.parametrize(
    "action,terminal", [("pause", "paused"), ("cancel", "canceled")]
)
def test_stalled_stream_pause_cancel_and_partial_resume(
    tmp_path, monkeypatch, action, terminal
):
    monkeypatch.setitem(TORRENT_HOSTS, "downloadresourcefake", ("torrent.test",))
    content = b"d4:infod4:name4:test6:pieces131072:" + b"x" * 131072 + b"ee"

    async def check(repo):
        reached = asyncio.Event()
        calls = []

        class Stalled(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield content[:65536]
                reached.set()
                await asyncio.Event().wait()

        def handler(request):
            calls.append(request.headers.get("range"))
            if len(calls) == 1:
                return httpx.Response(
                    200, stream=Stalled(), headers={"Content-Length": str(len(content))}
                )
            offset = int(
                request.headers["range"].removeprefix("bytes=").removesuffix("-")
            )
            return httpx.Response(
                206,
                content=content[offset:],
                headers={
                    "Content-Range": f"bytes {offset}-{len(content) - 1}/{len(content)}"
                },
            )

        tasks, _ = await repo.create_tasks(
            [
                TaskCreate(
                    type="torrent",
                    source="downloadresourcefake",
                    source_id="stream",
                    title="stalled",
                )
            ]
        )
        task_id = tasks[0]["id"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            manager = DownloadManager(
                repo,
                FakePool(http),
                replace(load_settings(), download_dir=str(tmp_path)),
            )
            await manager.start()
            try:
                await asyncio.wait_for(reached.wait(), 2)
                requested = await manager.control(task_id, action)
                assert (
                    requested["status"] == "downloading"
                    and requested["requested_action"] == action
                )
                await asyncio.wait_for(wait_status(repo, task_id, terminal), 1)
                file = (await repo.files(task_id))[0]
                assert (
                    file["downloaded_bytes"] == 65536
                    and (tmp_path / (file["path"] + ".part")).stat().st_size == 65536
                )
                assert (await repo.get_task(task_id))["retry_count"] == 0
                await manager.control(
                    task_id, "resume" if action == "pause" else "retry"
                )
                done = await wait_status(repo, task_id, "completed")
                assert (
                    tmp_path / done["destination"]
                ).read_bytes() == content and calls == [None, "bytes=65536-"]
            finally:
                await manager.stop()

    asyncio.run(repository_case(tmp_path, check))


def test_sse_stream_format_and_disconnect_cleanup():
    from app.downloads.api import download_events

    class RequestStub:
        async def is_disconnected(self):
            return False

    class ManagerStub:
        events = EventBus()

    async def check():
        manager = ManagerStub()
        response = await download_events(RequestStub(), manager)
        first = await anext(response.body_iterator)
        assert first.startswith("event: download.updated\ndata: ")
        manager.events.publish(
            "download.completed", {"id": "task", "status": "completed"}
        )
        event = await anext(response.body_iterator)
        assert event.startswith("event: download.completed\n")
        data = json.loads(event.split("data: ")[1])
        assert data["task_id"] == "task" and data["status"] == "completed"
        await response.body_iterator.aclose()
        assert not manager.events.subscribers

    asyncio.run(check())


def test_content_rejects_destination_escape(download_api, tmp_path):
    task = download_api.post("/api/downloads", json=chapter_task().model_dump()).json()
    download_api.portal.call(
        app.state.downloads.repo.update,
        task["id"],
        {"status": "completed", "destination": "../escape"},
    )
    assert download_api.get(f"/api/downloads/{task['id']}/content").status_code == 404
