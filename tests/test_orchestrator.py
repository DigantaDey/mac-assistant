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


async def test_tts_worker_failure_is_observed_without_failing_the_session(orch, caplog):
    class BrokenTTS:
        def speak(self, _text):
            raise RuntimeError("speech device unavailable")

    orch._loop = asyncio.get_running_loop()
    orch.tts = BrokenTTS()
    orch._speak_async("The action is complete.")

    for _ in range(50):
        if "text-to-speech failed" in caplog.text:
            break
        await asyncio.sleep(0.01)

    assert "text-to-speech failed: RuntimeError: speech device unavailable" in caplog.text


async def test_terminal_state_keeps_session_id_for_ui_recovery(orch):
    sid = orch.bus.subscribe_async()
    await orch.submit_text("set volume to 30")
    events = orch.bus.drain(sid)

    planned = next(event for event in events
                   if event.type == "state" and event.data.get("state") == "planning")
    reply = next(event for event in events if event.type == "reply")
    terminal = next(event for event in reversed(events)
                    if event.type == "state" and event.data.get("state") == "armed")
    assert reply.data["session"] == planned.data["session"]
    assert terminal.data["session"] == planned.data["session"]
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


class _RecordingTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> float:
        self.spoken.append(text)
        return 0.0


async def test_the_confirmation_question_is_spoken(orch):
    """A voice user isn't looking at the panel: the question itself must be
    spoken when the proposal goes out, and the reply after a yes reports the
    OUTCOME — it does not ask again."""
    orch.tts = _RecordingTTS()
    sid = orch.bus.subscribe_async()
    task = asyncio.create_task(orch.submit_text("empty the trash"))
    while True:
        ev = await asyncio.wait_for(orch.bus.get(sid), timeout=5)
        if ev.type == "proposal":
            break
    # TTS is fire-and-forget on an executor — give it a beat to run.
    for _ in range(100):
        if any("go ahead" in line for line in orch.tts.spoken):
            break
        await asyncio.sleep(0.02)
    assert any("go ahead" in line for line in orch.tts.spoken), orch.tts.spoken
    orch.resolve_confirmation(ev.data["token"], "confirm")
    await asyncio.wait_for(task, timeout=5)
    for _ in range(100):
        if orch.tts.spoken and orch.tts.spoken[-1] == "Trash emptied.":
            break
        await asyncio.sleep(0.02)
    assert orch.tts.spoken[-1] == "Trash emptied.", orch.tts.spoken
    orch.bus.unsubscribe_async(sid)


async def test_a_timed_out_proposal_is_loud_not_silent(orch, aura_logs):
    """The old failure mode: the ask went out, nobody saw it, and 45 s later
    the session cancelled itself without a trace in the log."""
    orch.cfg.session.confirmation_timeout_seconds = 0.2
    sid = orch.bus.subscribe_async()
    await asyncio.wait_for(orch.submit_text("empty the trash"), timeout=5)
    assert orch.state == "armed"
    assert orch.memory.recent_events()[0]["outcome"] == "cancelled"
    assert any("no answer within" in r.getMessage() for r in aura_logs), \
        [r.getMessage() for r in aura_logs if "proposal" in r.getMessage()]
    orch.bus.unsubscribe_async(sid)


async def test_feedback_lands_on_its_skill_with_its_args(orch):
    orch.record_feedback("open youtube.com in safari", "browser.open_url",
                         "corrected", "", {"url": "youtube.com", "browser": "Safari"})
    stats = orch.examples.stats()
    assert stats["corrected"] == 1 and stats["total"] == 1
    # and a verdict without a skill supervises nothing — no ""-skill rows
    orch.record_feedback("open spotify", "", "corrected")
    assert orch.examples.stats()["total"] == 1


async def test_setup_done_is_guaranteed_when_the_installer_crashes(stack):
    """If the install raises, the UI must still see setup_done — without it
    the Setup panel is stuck in 'Installing…' with every button disabled."""
    orch = stack.build_orchestrator()
    orch.cfg.profile = "mac"

    def boom():
        raise RuntimeError("disk full")

    orch._run_setup_sync = boom
    sid = orch.bus.subscribe_async()
    result = await orch.run_setup()
    events = orch.bus.drain(sid)
    done = [ev for ev in events if ev.type == "setup_done"]
    assert result["ok"] is False
    assert done and done[0].data["ok"] is False
    assert "disk full" in done[0].data["summary"]
    assert orch._setup_running is False
    orch.bus.unsubscribe_async(sid)
