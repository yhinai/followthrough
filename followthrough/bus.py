from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "type": event_type,
            "at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        # Interim ASR hypotheses arrive several times a second and are safe to
        # shed; everything else states a durable fact. When a slow consumer's
        # queue is full, drop the partial itself, but for durable events evict
        # the oldest queued entry (most likely a stale partial) so the fact
        # still reaches the consumer.
        droppable = event_type == "transcript_partial"
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                if droppable:
                    continue
                # Make room only by shedding an ephemeral hypothesis. Never
                # sacrifice one durable lifecycle fact for another.
                queued_partial = next(
                    (item for item in queue._queue if item["type"] == "transcript_partial"),
                    None,
                )
                if queued_partial is not None:
                    queue._queue.remove(queued_partial)
                    queue.put_nowait(event)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self._subscribers.discard(queue)


bus = EventBus()
