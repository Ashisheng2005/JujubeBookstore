"""Exercise the real frontend with offline API fixtures and save responsive screenshots.

Requires the optional Playwright Python package and a running JujubeBookstore server.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, expect

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZfkAAAAASUVORK5CYII="
)


def fixture_task(task_id, status, **kwargs):
    return {
        "id": task_id,
        "title": "JujubeBookstore Chapter " + task_id,
        "group_id": "group-1",
        "source": "mangabz",
        "type": "comic_chapter",
        "status": status,
        "cleanup_state": "none",
        "requested_action": "none",
        "progress": 100 if status == "completed" else 42.5,
        "file_count": 20,
        "completed_files": 20 if status == "completed" else 8,
        "downloaded_bytes": 5242880,
        "total_bytes": 12582912,
        "speed": 1048576 if status == "downloading" else 0,
        "eta": 7 if status == "downloading" else None,
        "retry_count": 0,
        "error_message": None,
        **kwargs,
    }


async def run(base_url, output, executable):
    output.mkdir(parents=True, exist_ok=True)
    tasks = [
        fixture_task("1", "downloading"),
        fixture_task("2", "paused"),
        fixture_task("3", "completed"),
        fixture_task("4", "failed", error_message="Upstream HTTP 404"),
    ]
    groups = [
        {"id": "group-1", "name": "Comics", "description": "", "save_path": "comics"}
    ]
    chapters = [
        {"id": str(i), "title": f"Chapter {i}", "group": "Serial"} for i in range(1, 4)
    ]
    errors = []

    async def route_api(route):
        request = route.request
        parsed = urlparse(request.url)
        path, query = parsed.path, parse_qs(parsed.query)
        method = request.method
        data = request.post_data_json if request.post_data else {}
        result, status = None, 200
        if path == "/api/downloads/events":
            await route.continue_()
            return
        if path == "/api/download-groups":
            if method == "POST":
                result = {"id": "group-" + str(len(groups) + 1), **data}
                groups.append(result)
            else:
                result = groups
        elif path.startswith("/api/download-groups/"):
            group = next(g for g in groups if g["id"] == path.rsplit("/", 1)[1])
            if method == "DELETE":
                groups.remove(group)
                for task in tasks:
                    if task["group_id"] == group["id"]:
                        task["group_id"] = None
                status = 204
            else:
                group.update(data)
                result = group
        elif path == "/api/downloads":
            if method == "POST":
                result = fixture_task(
                    str(len(tasks) + 1),
                    "queued",
                    title=data["title"],
                    type=data["type"],
                    source=data["source"],
                    group_id=data.get("group_id"),
                )
                tasks.append(result)
            else:
                items = tasks[:]
                for key in ("status", "type", "group_id"):
                    if key in query:
                        value = query[key][0]
                        items = [
                            t
                            for t in items
                            if (
                                t[key] is None
                                if value == "ungrouped"
                                else t[key] == value
                            )
                        ]
                page = int(query.get("page", ["1"])[0])
                size = int(query.get("page_size", ["25"])[0])
                result = {
                    "items": items[(page - 1) * size : page * size],
                    "total": len(items),
                    "page": page,
                    "page_size": size,
                    "counts": {
                        state: sum(t["status"] == state for t in tasks)
                        for state in ("queued", "downloading", "completed", "failed")
                    },
                }
        elif path == "/api/download-batches/preview":
            result = {
                "count": 3,
                "existing_count": 0,
                "new_count": 3,
                "items": [
                    {"chapter_id": c["id"], "title": c["title"], "exists": False}
                    for c in chapters
                ],
            }
        elif path == "/api/downloads/batch":
            new = [
                fixture_task(
                    str(len(tasks) + i + 1),
                    "queued",
                    title="Test comic " + c["title"],
                    group_id=data.get("group_id"),
                )
                for i, c in enumerate(chapters)
            ]
            tasks.extend(new)
            result = {
                "batch_id": "batch",
                "task_ids": [t["id"] for t in new],
                "created_count": 3,
            }
        elif path.startswith("/api/downloads/"):
            parts = path.split("/")
            task = next(t for t in tasks if t["id"] == parts[3])
            action = parts[4] if len(parts) > 4 else None
            if method == "DELETE":
                tasks.remove(task)
                status = 204
            elif action in ("pause", "resume", "cancel", "retry"):
                task["status"] = {
                    "pause": "paused",
                    "resume": "queued",
                    "cancel": "canceled",
                    "retry": "queued",
                }[action]
                result = task
            elif action == "files":
                result = [
                    {
                        "name": "001.jpg",
                        "status": "completed",
                        "size": 524288,
                        "downloaded_bytes": 524288,
                    }
                ]
            elif action == "content":
                await route.fulfill(
                    status=200,
                    body=b"fixture CBZ",
                    headers={
                        "Content-Type": "application/zip",
                        "Content-Disposition": "attachment; filename=test.cbz",
                    },
                )
                return
            else:
                result = task
        elif path == "/api/mangabz/search":
            result = {
                "source": "mangabz",
                "keyword": "Test",
                "count": 1,
                "items": [{"id": "1", "title": "Test comic"}],
            }
        elif path == "/api/mangabz/comic/1":
            result = {"id": "1", "title": "Test comic", "chapters": chapters}
        elif "/chapter/" in path:
            result = {
                "count": 1,
                "title": "Chapter 1",
                "images": ["https://fixture.test/image.png"],
            }
        elif path == "/api/mangabz/image":
            await route.fulfill(status=200, body=PNG, content_type="image/png")
            return
        elif path == "/api/resources/dmhy/search":
            result = {
                "source": "dmhy",
                "keyword": "Test",
                "count": 1,
                "items": [{"id": "item", "title": "Test torrent"}],
            }
        elif path == "/api/resources/dmhy/item/item":
            result = {
                "id": "item",
                "title": "Test torrent",
                "files": [],
                "torrent": "https://dl.dmhy.org/file.torrent",
            }
        else:
            await route.continue_()
            return
        await route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(result) if status != 204 else "",
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, **({"executable_path": executable} if executable else {})
        )
        context = await browser.new_context(viewport={"width": 1440, "height": 960})
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/**", route_api)
        await page.goto(base_url)
        await page.locator('#source option[value="mangabz"]').wait_for(state="attached")
        await page.locator('nav [data-mode="downloads"]').click()
        await expect(page.locator("#download-rows tr")).to_have_count(4)
        await page.locator('[data-detail-task="4"]').click()
        await expect(page.locator("#download-detail")).to_contain_text(
            "Upstream HTTP 404"
        )
        await page.screenshot(
            path=str(output / "downloads-desktop.png"), full_page=True
        )
        await page.locator('[data-task="2"][data-action="resume"]').first.click()
        await expect(
            page.locator('[data-task="2"][data-action="pause"]').first
        ).to_be_visible()
        await page.locator('[data-select-task="2"]').check()
        await page.locator('[data-bulk="pause"]').click()
        await expect(
            page.locator('[data-task="2"][data-action="resume"]').first
        ).to_be_visible()
        await page.locator("#select-all-tasks").check()
        await expect(page.locator("#bulk-actions")).to_contain_text("4")
        await page.locator("#select-all-tasks").uncheck()
        async with page.expect_download():
            await page.locator(
                '#download-rows a[href="/api/downloads/3/content"]'
            ).click()
        await page.locator("#new-download-group").click()
        await page.locator("#group-name").fill("New group")
        await page.locator("#group-path").fill("new/comics")
        await page.locator("#dialog-submit").click()
        await expect(page.locator('[data-group="group-2"]')).to_be_visible()
        await page.locator('[data-group="group-2"]').click()
        await page.locator("#edit-download-group").click()
        await page.locator("#group-name").fill("Edited group")
        await page.locator("#dialog-submit").click()
        await expect(page.locator('[data-group="group-2"]')).to_have_text(
            "Edited group"
        )
        await page.locator("#delete-download-group").click()
        await page.locator("#dialog-submit").click()
        await expect(page.locator('[data-group="group-2"]')).to_have_count(0)
        await page.locator('nav [data-mode="comic"]').click()
        await page.locator("#source").select_option("mangabz")
        await page.locator("#q").fill("Test")
        await page.locator("#go").click()
        await page.locator(".card").click()
        await page.locator('.chap[data-id="1"]').click()
        await page.locator("#download-chapter").click()
        await page.locator("#task-group").select_option("group-1")
        await page.locator("#dialog-submit").click()
        await expect(page.locator("#download-rows tr")).to_have_count(5)
        await page.locator('nav [data-mode="comic"]').click()
        await page.locator("#source").select_option("mangabz")
        await page.locator("#q").fill("Test")
        await page.locator("#go").click()
        await page.locator(".card").click()
        await page.locator("[data-download-group]").click()
        await expect(page.locator("#download-dialog")).to_contain_text("3")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.screenshot(path=str(output / "batch-mobile.png"), full_page=True)
        await page.locator("#dialog-submit").click()
        await expect(page.locator("#download-rows tr")).to_have_count(8)
        await page.locator("[data-bulk-select-all]").check()
        await expect(page.locator("#bulk-actions")).to_contain_text("8")
        await page.locator("[data-bulk-select-all]").uncheck()
        await page.screenshot(path=str(output / "downloads-mobile.png"), full_page=True)
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth"
        ), "Page overflows the mobile viewport"
        await page.locator('nav [data-mode="resource"]').click()
        await page.locator("#source").select_option("dmhy")
        await page.locator("#q").fill("Test")
        await page.locator("#go").click()
        await page.locator("[data-detail]").click()
        await page.locator("#download-torrent").click()
        await page.locator("#dialog-submit").click()
        await expect(page.locator("#download-rows tr")).to_have_count(9)
        assert not errors, errors
        await context.close()
        await browser.close()
    print(f"Download UI flows passed; screenshots: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(tempfile.gettempdir()) / "jujube-download-ui",
    )
    parser.add_argument("--executable", default=None)
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.output, args.executable))
