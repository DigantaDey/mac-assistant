"""A tiny asynchronous pub/sub event bus.

Every interesting thing Aura does is published here; the local UI subscribes
over SSE, the memory layer records, and tests assert on the stream. One
simple primitive keeps the whole system observable.
"""

from __future__ import annotations

import asyncio
import itertools
import queue
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data}


class EventBus:
    """Fan-out to async subscribers and thread-safe SSE queues.

    `subscribe_async`  → consumers living on the orchestrator's loop (tests, log).
    `subscribe_queue`  → blocking consumers on other threads (SSE handlers).
    """

    def __init__(self, history: int = 500) -> None:
        self._async_subs: dict[int, asyncio.Queue[Event]] = {}
        self._queue_subs: dict[int, "queue.SimpleQueue[Event]"] = {}
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._history: list[Event] = []
        self._history_max = history

    # -- subscriptions ------------------------------------------------------

    def subscribe_async(self) -> int:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        sid = next(self._ids)
        self._async_subs[sid] = q
        return sid

    def unsubscribe_async(self, sid: int) -> None:
        self._async_subs.pop(sid, None)

    def subscribe_queue(self) -> tuple[int, "queue.SimpleQueue[Event]"]:
        q: queue.SimpleQueue[Event] = queue.SimpleQueue()
        sid = next(self._ids)
        self._queue_subs[sid] = q
        return sid, q

    def unsubscribe_queue(self, sid: int) -> None:
        self._queue_subs.pop(sid, None)

    # -- publishing ---------------------------------------------------------

    def publish(self, type_: str, **data: Any) -> Event:
        ev = Event(type=type_, data=data, seq=next(self._seq))
        self._history.append(ev)
        if len(self._history) > self._history_max:
            del self._history[: len(self._history) - self._history_max]
        for q in self._async_subs.values():
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:  # a slow subscriber must never stall the bus
                pass
        for q in self._queue_subs.values():
            q.put(ev)
        return ev

    # -- reading ------------------------------------------------------------

    async def get(self, sid: int) -> Event:
        return await self._async_subs[sid].get()

    def recent(self, limit: int = 100) -> list[Event]:
        return list(self._history[-limit:])

    def drain(self, sid: int) -> list[Event]:
        """Pop everything currently queued for an async subscriber."""
        out: list[Event] = []
        q = self._async_subs[sid]
        while True:
            try:
                out.append(q.get_nowait())
            except asyncio.QueueEmpty:
                return out
