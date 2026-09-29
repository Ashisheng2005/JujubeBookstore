from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager


class Subscription:
    def __init__(self, limit: int):
        self.limit = limit
        self.pending = OrderedDict()
        self.ready = asyncio.Event()

    def put(self, event):
        key = event.get("task_id", "snapshot")
        self.pending[key] = event
        if len(self.pending) > self.limit:
            self.pending.popitem(last=False)
        self.ready.set()

    async def get(self):
        await self.ready.wait()
        _, event = self.pending.popitem(last=False)
        if not self.pending:
            self.ready.clear()
        return event


class EventBus:
    def __init__(self, queue_limit: int = 100):
        self.queue_limit = queue_limit
        self.subscribers: set[Subscription] = set()

    @asynccontextmanager
    async def subscribe(self):
        subscription = Subscription(self.queue_limit)
        self.subscribers.add(subscription)
        try:
            yield subscription
        finally:
            self.subscribers.discard(subscription)

    def publish(self, event_type, task=None):
        event = {"type": event_type}
        if task:
            event.update(
                {
                    key: value
                    for key, value in task.items()
                    if key not in {"type", "payload", "idempotency_key"}
                }
            )
            if "type" in task:
                event["task_type"] = task["type"]
            event["task_id"] = task["id"]
        for subscriber in self.subscribers:
            subscriber.put(event)
