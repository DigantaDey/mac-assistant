"""EventBus ordering and SSE resume semantics."""

from __future__ import annotations

import threading

from aura.events import EventBus


def test_fresh_stream_subscriber_receives_only_live_events():
    bus = EventBus()
    old = bus.publish("reply", text="previous session")

    sid, live = bus.subscribe_queue()
    assert live.empty()

    current = bus.publish("reply", text="current session")
    assert live.get_nowait() == current
    assert current.seq > old.seq
    bus.unsubscribe_queue(sid)


def test_resume_cursor_replays_backlog_and_then_live_events_once():
    bus = EventBus()
    first = bus.publish("state", state="planning")
    replayed = bus.publish("reply", text="finished")

    sid, live, backlog = bus.subscribe_queue_after(first.seq)
    assert backlog == [replayed]

    after_resume = bus.publish("state", state="armed")
    assert live.get_nowait() == after_resume
    assert live.empty()
    bus.unsubscribe_queue(sid)


def test_concurrent_publish_keeps_sse_sequence_order():
    bus = EventBus()
    sid, live = bus.subscribe_queue()

    def publish_batch(batch: int) -> None:
        for index in range(100):
            bus.publish("log", batch=batch, index=index)

    writers = [threading.Thread(target=publish_batch, args=(batch,)) for batch in range(4)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()

    events = [live.get_nowait() for _ in range(400)]
    assert [event.seq for event in events] == sorted(event.seq for event in events)
    bus.unsubscribe_queue(sid)
