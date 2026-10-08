"""A tiny asynchronous pub/sub event bus.

Every interesting thing Aura does is published here; the local UI subscribes
over SSE, the memory layer records, and tests assert on the stream. One
simple primitive keeps the whole system observable.
"""

from __future__ import annotations

import asyncio
import itertools
import queue
import threading
import time
import uuid
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
        self._queue_subs: dict[int, queue.SimpleQueue[Event]] = {}
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._history: list[Event] = []
        self._history_max = history
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # A new engine process starts a new sequence. The SSE epoch lets a
        # reconnecting native client distinguish that from an old cursor.
        self.epoch = uuid.uuid4().hex

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """The loop that owns the async subscriber queues. `publish()` may be
        called from other threads (the HTTP server) — delivery to asyncio
        queues is always scheduled back onto this loop."""
        self._loop = loop

    # -- subscriptions ------------------------------------------------------

    def subscribe_async(self) -> int:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        sid = next(self._ids)
        with self._lock:
            self._async_subs[sid] = q
        return sid

    def unsubscribe_async(self, sid: int) -> None:
        with self._lock:
            self._async_subs.pop(sid, None)

    def subscribe_queue(self) -> tuple[int, queue.SimpleQueue[Event]]:
        q: queue.SimpleQueue[Event] = queue.SimpleQueue()
        sid = next(self._ids)
        with self._lock:
            self._queue_subs[sid] = q
        return sid, q

    def subscribe_queue_after(
        self, after_seq: int,
    ) -> tuple[int, queue.SimpleQueue[Event], list[Event]]:
        """Subscribe atomically and return events newer than ``after_seq``.

        Registering the live queue and taking the backlog under the same lock
        closes the replay/live race: each event is delivered once, either in
        the backlog or in the queue, never lost between the two.
        """
        q: queue.SimpleQueue[Event] = queue.SimpleQueue()
        sid = next(self._ids)
        with self._lock:
            backlog = [event for event in self._history if event.seq > after_seq]
            self._queue_subs[sid] = q
        return sid, q, backlog

    def unsubscribe_queue(self, sid: int) -> None:
        with self._lock:
            self._queue_subs.pop(sid, None)

    # -- publishing ---------------------------------------------------------

    def publish(self, type_: str, **data: Any) -> Event:
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None

        with self._lock:
            ev = Event(type=type_, data=data, seq=next(self._seq))
            self._history.append(ev)
            if len(self._history) > self._history_max:
                del self._history[: len(self._history) - self._history_max]
            async_qs = list(self._async_subs.values())
            queue_qs = list(self._queue_subs.values())

            # Preserve sequence order for SSE subscribers even when multiple
            # worker threads publish concurrently.
            for q in queue_qs:
                q.put(ev)

            def deliver_async() -> None:
                for q in async_qs:
                    try:
                        q.put_nowait(ev)
                    except asyncio.QueueFull:  # slow subscriber must not stall the bus
                        pass

            if self._loop is not None and here is not self._loop:
                try:
                    self._loop.call_soon_threadsafe(deliver_async)
                except RuntimeError:  # loop closed during shutdown
                    pass
            else:
                deliver_async()

        return ev

    # -- reading ------------------------------------------------------------

    async def get(self, sid: int) -> Event:
        return await self._async_subs[sid].get()

    def recent(self, limit: int = 100) -> list[Event]:
        with self._lock:
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
