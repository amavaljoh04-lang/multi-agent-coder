"""In-memory pub/sub for WebSocket streaming.

Every time an agent emits a line, we fan it out to every connected WebSocket
subscribed to that project. The event is ALSO persisted to the DB by the
orchestrator so reconnecting clients can replay history.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, project_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        async with self._lock:
            self._queues[project_id].append(q)
        return q

    async def unsubscribe(self, project_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            if q in self._queues.get(project_id, []):
                self._queues[project_id].remove(q)

    async def publish(self, project_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._queues.get(project_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest to make room — UI-level events are cheap to lose.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass

    async def stream(self, project_id: str) -> AsyncIterator[dict[str, Any]]:
        q = await self.subscribe(project_id)
        try:
            while True:
                yield await q.get()
        finally:
            await self.unsubscribe(project_id, q)


bus = EventBus()
