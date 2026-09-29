from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite

from .models import GroupCreate, GroupUpdate, TaskCreate


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS download_groups (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 save_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_tasks (
 id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
 group_id TEXT REFERENCES download_groups(id) ON DELETE SET NULL,
 type TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}', title TEXT NOT NULL,
 output_format TEXT NOT NULL DEFAULT 'cbz', status TEXT NOT NULL,
 requested_action TEXT NOT NULL DEFAULT 'none', cleanup_state TEXT NOT NULL DEFAULT 'none',
 batch_id TEXT, total_bytes INTEGER, downloaded_bytes INTEGER NOT NULL DEFAULT 0,
 file_count INTEGER NOT NULL DEFAULT 0, completed_files INTEGER NOT NULL DEFAULT 0,
 progress REAL NOT NULL DEFAULT 0, speed REAL NOT NULL DEFAULT 0, eta INTEGER,
 retry_count INTEGER NOT NULL DEFAULT 0, error_message TEXT, destination TEXT,
 created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_files (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES download_tasks(id) ON DELETE CASCADE,
 name TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, downloaded_bytes INTEGER NOT NULL DEFAULT 0,
 checksum TEXT, status TEXT NOT NULL DEFAULT 'pending', UNIQUE(task_id, name)
);
CREATE TABLE IF NOT EXISTS download_batches (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS download_batch_tasks (
 batch_id TEXT NOT NULL REFERENCES download_batches(id) ON DELETE CASCADE,
 task_id TEXT NOT NULL REFERENCES download_tasks(id) ON DELETE CASCADE,
 PRIMARY KEY(batch_id, task_id)
);
CREATE INDEX IF NOT EXISTS download_tasks_queue ON download_tasks(status, created_at);
CREATE INDEX IF NOT EXISTS download_tasks_group ON download_tasks(group_id);
PRAGMA user_version=1;
"""


class MissingRecord(Exception):
    pass


class StateConflict(Exception):
    pass


class DownloadRepository:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.db: aiosqlite.Connection | None = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA busy_timeout=5000")
        async with self.db.execute("PRAGMA user_version") as cursor:
            version = (await cursor.fetchone())[0]
        if version > 1:
            raise RuntimeError("Download database was created by a newer version")
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None

    async def _rows(self, sql, params=()):
        async with self.db.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def _task(row):
        if row:
            row["payload"] = json.loads(row.pop("payload_json"))
        return row

    async def groups(self):
        async with self.lock:
            return await self._rows(
                "SELECT * FROM download_groups ORDER BY created_at, id"
            )

    async def group(self, group_id):
        rows = await self._rows("SELECT * FROM download_groups WHERE id=?", (group_id,))
        if not rows:
            raise MissingRecord("Download group not found")
        return rows[0]

    async def create_group(self, data: GroupCreate):
        async with self.lock:
            group_id, timestamp = str(uuid4()), now()
            await self.db.execute(
                "INSERT INTO download_groups VALUES(?,?,?,?,?,?)",
                (
                    group_id,
                    data.name,
                    data.description,
                    data.save_path,
                    timestamp,
                    timestamp,
                ),
            )
            await self.db.commit()
            return await self.group(group_id)

    async def update_group(self, group_id, data: GroupUpdate):
        async with self.lock:
            await self.group(group_id)
            values = data.model_dump(exclude_unset=True)
            values["updated_at"] = now()
            await self.db.execute(
                "UPDATE download_groups SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE id=?",
                (*values.values(), group_id),
            )
            await self.db.commit()
            return await self.group(group_id)

    async def delete_group(self, group_id):
        async with self.lock:
            await self.group(group_id)
            await self.db.execute("DELETE FROM download_groups WHERE id=?", (group_id,))
            await self.db.commit()

    async def get_task(self, task_id):
        async with self.lock:
            return await self._get_task(task_id)

    async def _get_task(self, task_id):
        rows = await self._rows("SELECT * FROM download_tasks WHERE id=?", (task_id,))
        if not rows:
            raise MissingRecord("Download task not found")
        return self._task(rows[0])

    async def tasks(
        self, *, group_id=None, status=None, type=None, page=1, page_size=50
    ):
        clauses, params = [], []
        for key, value in (("group_id", group_id), ("status", status), ("type", type)):
            if value is not None:
                if key == "group_id" and value == "ungrouped":
                    clauses.append("group_id IS NULL")
                else:
                    clauses.append(f"{key}=?")
                    params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        async with self.lock:
            total = (
                await self._rows(
                    "SELECT COUNT(*) AS n FROM download_tasks" + where, params
                )
            )[0]["n"]
            rows = await self._rows(
                "SELECT * FROM download_tasks"
                + where
                + " ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            )
            counts = await self._rows(
                "SELECT status, COUNT(*) AS n FROM download_tasks GROUP BY status"
            )
        return {
            "items": [self._task(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "counts": {row["status"]: row["n"] for row in counts},
        }

    async def create_tasks(self, items: list[TaskCreate], batch_id: str | None = None):
        results, created = [], []
        async with self.lock:
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                if batch_id:
                    await self.db.execute(
                        "INSERT INTO download_batches VALUES(?,?)", (batch_id, now())
                    )
                for data in items:
                    save_path = None
                    if data.group_id:
                        save_path = (await self.group(data.group_id))["save_path"]
                    key = data.idempotency_key()
                    existing = await self._rows(
                        "SELECT * FROM download_tasks WHERE idempotency_key=?", (key,)
                    )
                    if existing:
                        task = self._task(existing[0])
                        if task["cleanup_state"] == "pending":
                            raise StateConflict("Task is being deleted")
                        if task["status"] == "canceled":
                            await self.db.execute(
                                "UPDATE download_tasks SET status='queued', requested_action='none', "
                                "retry_count=0, error_message=NULL, updated_at=? WHERE id=?",
                                (now(), task["id"]),
                            )
                            task = await self._get_task(task["id"])
                            created.append(task)
                    else:
                        task_id, timestamp = str(uuid4()), now()
                        await self.db.execute(
                            "INSERT INTO download_tasks(id,idempotency_key,group_id,type,source,source_id,"
                            "payload_json,title,output_format,status,batch_id,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
                            (
                                task_id,
                                key,
                                data.group_id,
                                data.type,
                                data.source,
                                data.source_id,
                                json.dumps({**data.payload(), "save_path": save_path}),
                                data.title,
                                data.output_format,
                                batch_id,
                                timestamp,
                                timestamp,
                            ),
                        )
                        task = await self._get_task(task_id)
                        created.append(task)
                    if batch_id:
                        # Reused tasks can belong to multiple batches without changing their original batch.
                        await self.db.execute(
                            "INSERT OR IGNORE INTO download_batch_tasks VALUES(?,?)",
                            (batch_id, task["id"]),
                        )
                    results.append(task)
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return results, created

    async def existing_keys(self, keys):
        async with self.lock:
            rows = await self._rows(
                "SELECT idempotency_key FROM download_tasks WHERE status!='canceled'"
            )
        return set(keys) & {row["idempotency_key"] for row in rows}

    async def batch(self, batch_id):
        async with self.lock:
            if not await self._rows(
                "SELECT id FROM download_batches WHERE id=?", (batch_id,)
            ):
                raise MissingRecord("Download batch not found")
            rows = await self._rows(
                "SELECT t.* FROM download_tasks t JOIN download_batch_tasks b ON t.id=b.task_id "
                "WHERE b.batch_id=? ORDER BY t.created_at, t.id",
                (batch_id,),
            )
        tasks = [self._task(row) for row in rows]
        counts = {
            state: sum(t["status"] == state for t in tasks)
            for state in (
                "queued",
                "downloading",
                "paused",
                "completed",
                "failed",
                "canceled",
            )
        }
        return {
            "batch_id": batch_id,
            "total": len(tasks),
            "counts": counts,
            "items": tasks,
        }

    async def update(self, task_id, values, *, statuses=None, action=None):
        allowed = {
            "status",
            "requested_action",
            "cleanup_state",
            "total_bytes",
            "downloaded_bytes",
            "file_count",
            "completed_files",
            "progress",
            "speed",
            "eta",
            "retry_count",
            "error_message",
            "destination",
            "started_at",
            "completed_at",
        }
        if not set(values) <= allowed:
            raise ValueError("Invalid task fields")
        values = {**values, "updated_at": now()}
        where, params = "id=?", [*values.values(), task_id]
        if statuses:
            where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params += list(statuses)
        if action is not None:
            where += " AND requested_action=?"
            params.append(action)
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE download_tasks SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE "
                + where,
                params,
            )
            await self.db.commit()
            return cursor.rowcount == 1

    async def control(self, task_id, action):
        async with self.lock:
            task = await self._get_task(task_id)
            status = task["status"]
            if task["cleanup_state"] != "none":
                raise StateConflict("Task is being deleted")
            if action in {"pause", "cancel"}:
                allowed = {"queued", "downloading"} | (
                    {"paused", "pending"} if action == "cancel" else set()
                )
                if status not in allowed or task["requested_action"] == "cancel":
                    raise StateConflict(f"Cannot {action} a {status} task")
                new_status = (
                    status
                    if status == "downloading"
                    else ("paused" if action == "pause" else "canceled")
                )
                requested = action if status == "downloading" else "none"
                values = {
                    "status": new_status,
                    "requested_action": requested,
                    "speed": 0,
                    "eta": None,
                }
            else:
                allowed = {"paused"} if action == "resume" else {"failed", "canceled"}
                if status not in allowed:
                    raise StateConflict(f"Cannot {action} a {status} task")
                values = {
                    "status": "queued",
                    "requested_action": "none",
                    "error_message": None,
                    "retry_count": 0 if action == "retry" else task["retry_count"],
                }
            values["updated_at"] = now()
            await self.db.execute(
                "UPDATE download_tasks SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE id=?",
                (*values.values(), task_id),
            )
            await self.db.commit()
            return await self._get_task(task_id)

    async def files(self, task_id):
        async with self.lock:
            return await self._rows(
                "SELECT * FROM download_files WHERE task_id=? ORDER BY name", (task_id,)
            )

    async def save_file(
        self,
        task_id,
        name,
        path,
        *,
        size=None,
        downloaded_bytes=0,
        checksum=None,
        status="pending",
    ):
        async with self.lock:
            await self.db.execute(
                "INSERT INTO download_files VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_id,name) "
                "DO UPDATE SET path=excluded.path,size=excluded.size,downloaded_bytes=excluded.downloaded_bytes,"
                "checksum=excluded.checksum,status=excluded.status",
                (
                    str(uuid4()),
                    task_id,
                    name,
                    path,
                    size,
                    downloaded_bytes,
                    checksum,
                    status,
                ),
            )
            await self.db.commit()

    async def remove_file_record(self, task_id, name):
        async with self.lock:
            await self.db.execute(
                "DELETE FROM download_files WHERE task_id=? AND name=?", (task_id, name)
            )
            await self.db.commit()

    async def recovery(self):
        async with self.lock:
            await self.db.execute(
                "UPDATE download_tasks SET status='queued',requested_action='none',speed=0,eta=NULL,updated_at=? "
                "WHERE status IN ('pending','downloading')",
                (now(),),
            )
            await self.db.execute(
                "UPDATE download_tasks SET speed=0,eta=NULL WHERE status!='downloading'"
            )
            await self.db.commit()
            return [
                self._task(row)
                for row in await self._rows(
                    "SELECT * FROM download_tasks ORDER BY created_at, id"
                )
            ]

    async def mark_cleanup(self, task_id):
        async with self.lock:
            task = await self._get_task(task_id)
            if task["status"] not in {"completed", "failed", "canceled"}:
                raise StateConflict("Cancel the task before deleting it")
            await self.db.execute(
                "UPDATE download_tasks SET cleanup_state='pending',updated_at=? WHERE id=?",
                (now(), task_id),
            )
            await self.db.commit()
            return task

    async def delete_task(self, task_id):
        async with self.lock:
            await self.db.execute(
                "DELETE FROM download_tasks WHERE id=? AND cleanup_state='pending'",
                (task_id,),
            )
            await self.db.commit()
