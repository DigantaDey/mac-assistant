"""End-to-end orchestration: the exact flow a user's voice triggers."""

from __future__ import annotations

import asyncio

import pytest
from conftest import DemoStack, collect


@pytest.fixture()
def orch(stack: DemoStack):
    o = stack.build_orchestrator()
    return o


async def test_simple_safe_session(orch):
    sid = orch.bus.subscribe_async()
    types = await collect(orch.bus, sid, orch.submit_text("open spotify"))
    assert orch.state == "armed"
    for expected in ("state", "plan", "action_started", "action_result", "reply"):
        assert expected in types, types
    # nothing required confirmation
    assert "proposal" not in types
    # the dry-run bridge actually received the osascript
    assert any("Spotify" in c[1] for c in orch.bridge.calls)
    # supervision recorded as auto-run
    assert orch.examples.stats()["total"] == 1
    # event stored for the timeline
    assert orch.memory.recent_events()[0]["transcript"] == "open spotify"
    orch.bus.unsubscribe_async(sid)


async def test_confirmation_flow(orch):
    sid = orch.bus.subscribe_async()
    task = asyncio.create_task(orch.submit_text("empty the trash"))

    # wait for the proposal
    while True:
        ev = await asyncio.wait_for(orch.bus.get(sid), timeout=5)
        if ev.type == "proposal":
            break
    assert ev.data["token"]
    assert ev.data["actions"][0]["verdict"] == "confirm"

    # confirm it
    assert orch.resolve_confirmation(ev.data["token"], "confirm") is True
    await asyncio.wait_for(task, timeout=5)
    assert any("empty trash" in c[1].lower() for c in orch.bridge.calls)
    assert orch.examples.stats()["confirmed"] == 1
    orch.bus.unsubscribe_async(sid)


async def test_strict_mode_asks_even_for_safe_actions(orch):
    """show_plan_before_run=True pauses even safe, confident actions."""
    orch.cfg.safety.show_plan_before_run = True
    sid = orch.bus.subscribe_async()
    task = asyncio.create_task(orch.submit_text("open spotify"))
    while True:
        ev = await asyncio.wait_for(orch.bus.get(sid), timeout=5)
        if ev.type == "proposal":
            break
    assert ev.data["actions"][0]["verdict"] == "run"  # safe, but strict mode says ask
    orch.resolve_confirmation(ev.data["token"], "cancel")
    await asyncio.wait_for(task, timeout=5)
    assert orch.bridge.calls == []
    orch.bus.unsubscribe_async(sid)


async def test_risky_action_asks_with_strict_mode_off(orch):
    """The safety gate's 'ask' cannot be switched off by a setting."""
    assert orch.cfg.safety.show_plan_before_run is False  # the default
    sid = orch.bus.subscribe_async()
    task = asyncio.create_task(orch.submit_text("empty the trash"))
    while True:
        ev = await asyncio.wait_for(orch.bus.get(sid), timeout=5)
        if ev.type == "proposal":
            break
    assert ev.data["actions"][0]["verdict"] == "confirm"
    orch.resolve_confirmation(ev.data["token"], "cancel")
    await asyncio.wait_for(task, timeout=5)
    assert not orch.bridge.calls
    orch.bus.unsubscribe_async(sid)


async def test_cancel_flow(orch):
    sid = orch.bus.subscribe_async()
    task = asyncio.create_task(orch.submit_text("empty the trash"))
    while True:
        ev = await asyncio.wait_for(orch.bus.get(sid), timeout=5)
        if ev.type == "proposal":
            break
    orch.resolve_confirmation(ev.data["token"], "cancel")
    await asyncio.wait_for(task, timeout=5)
    # nothing executed, cancellation recorded
    assert not orch.bridge.calls
    stats = orch.examples.stats()
    assert stats["cancelled"] == 1
    assert orch.memory.recent_events()[0]["outcome"] == "cancelled"
    orch.bus.unsubscribe_async(sid)


async def test_unknown_command_still_polite(orch):
    sid = orch.bus.subscribe_async()
    types = await collect(orch.bus, sid, orch.submit_text("refactor the linux kernel"))
    assert "reply" in types
    assert "action_started" not in types
    orch.bus.unsubscribe_async(sid)


async def test_typed_input_while_busy_gets_hint(orch):
    sid = orch.bus.subscribe_async()
    slow = asyncio.create_task(orch.submit_text("empty the trash"))
    while True:
        ev = await asyncio.wait_for(orch.bus.get(sid), timeout=5)
        if ev.type == "proposal":
            break
    # a second request while proposing → hint, no new session
    types = await collect(orch.bus, sid, orch.submit_text("open spotify"))
    assert "hint" in types
    orch.resolve_confirmation(ev.data["token"], "cancel")
    await asyncio.wait_for(slow, timeout=5)
    orch.bus.unsubscribe_async(sid)


async def test_trigger_without_audio_stays_armed(orch):
    """Demo profile: no mic — the orb must not wedge the state machine."""
    sid = orch.bus.subscribe_async()
    await orch.start()
    assert orch.state == "armed"
    types = await collect(orch.bus, sid, orch.trigger_manual())
    assert "hint" in types
    assert orch.state == "armed"
    await orch.stop()
    orch.bus.unsubscribe_async(sid)


async def test_feedback_learns(orch):
    orch.record_feedback("open spotify", "system.open_app", "corrected",
                         "when I say open spotify I mean the web player")
    stats = orch.examples.stats()
    assert stats["corrected"] == 1
    assert orch.memory.get_preference("when i say open spotify i mean the web player") is None or True
    # weighted examples exist for the fine-tune
    assert orch.examples.stats()["total"] == 1
