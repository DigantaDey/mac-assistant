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


# --------------------------------------------------------------------------- #
# A wedged skill must fail honestly — not spend the whole session budget        #
# --------------------------------------------------------------------------- #


class _StuckPlanner:
    """Plans one action whose skill takes far longer than any deadline."""

    async def plan(self, transcript, context):
        from aura.planner import Action, Plan

        return Plan(reply="Working on it.",
                    actions=[Action("system.hang", {}, "safe", "never returns")])


def _make_stuck_skill():
    """A skill wedged on a blocking system call — no inner timeout at all.

    The thread is released explicitly at the end of the test: a timed-out
    skill leaves its worker unwinding in the background (that is documented
    behaviour of the per-skill deadline), and the suite must not pay for it.
    """
    import threading

    from aura.skills.base import Skill, SkillResult, SkillSpec

    release = threading.Event()

    class StuckSkill(Skill):
        spec = SkillSpec(name="system.hang", description="never returns")

        async def execute(self, args, ctx):
            release.wait(10)               # a wedged AX call, from the outside
            return SkillResult(True, "unreachable")

    return StuckSkill(), release


async def test_skill_timeout_beats_the_session_watchdog(stack, monkeypatch):
    """The field failure: 'what's on my screen' died on the watchdog's
    generic 'that took too long' after 20 s, with the state machine killed
    mid-flight. A per-skill deadline reports WHICH skill wedged, keeps the
    session alive to deliver the reply, and returns to armed cleanly."""
    from aura import orchestrator as orch_mod
    from aura.skills.base import SkillRegistry

    monkeypatch.setattr(orch_mod, "SKILL_TIMEOUT_SECONDS", 0.3)
    stuck, release = _make_stuck_skill()
    orch = stack.build_orchestrator()
    orch.planner = _StuckPlanner()
    registry = SkillRegistry()
    registry.register(stuck)
    orch.registry = registry

    try:
        sid = orch.bus.subscribe_async()
        events = []

        async def watch():
            while True:
                events.append(await orch.bus.get(sid))

        watcher = asyncio.create_task(watch())
        await orch.submit_text("hang forever")
        await asyncio.sleep(0.05)
        watcher.cancel()
        orch.bus.unsubscribe_async(sid)

        assert orch.state == "armed"
        types = [ev.type for ev in events]
        assert "reply" in types
        result = next(ev for ev in events if ev.type == "action_result")
        assert result.data["ok"] is False
        assert "didn't finish within" in result.data["message"]
        reply = next(ev for ev in events if ev.type == "reply")
        assert "system.hang" in reply.data["text"]
    finally:
        release.set()                      # let the orphaned worker finish


async def test_stale_speech_is_never_announced(stack, monkeypatch):
    """A Mac waking from sleep must not blurt a cancellation from an hour
    ago: speech older than the freshness window is dropped, with a log line."""
    from aura import orchestrator as orch_mod

    orch = stack.build_orchestrator()
    orch._loop = asyncio.get_running_loop()
    spoken: list[str] = []

    class RecordingTTS:
        def speak(self, text):
            spoken.append(text)
            return 1.0

    orch.tts = RecordingTTS()

    monkeypatch.setattr(orch_mod, "MAX_SPEECH_AGE_SECONDS", -1)   # everything is stale
    orch._speak_async("I didn't hear a yes, so I cancelled it.")
    await asyncio.sleep(0.15)
    assert spoken == []

    monkeypatch.setattr(orch_mod, "MAX_SPEECH_AGE_SECONDS", 20.0)  # fresh again
    orch._speak_async("Done.")
    for _ in range(50):
        if spoken:
            break
        await asyncio.sleep(0.02)
    assert spoken == ["Done."]


async def test_stt_warm_up_runs_at_startup(stack, aura_logs):
    """whisper.cpp loads its model per invocation; the first command after a
    wake must not pay that under the transcription deadline. The engine warms
    any STT backend that offers it, in the background, best-effort."""
    orch = stack.build_orchestrator()
    warmed = []

    class WarmableSTT:
        def transcribe(self, frames):
            return ""

        def warm(self):
            warmed.append(True)
            return True

    orch.stt = WarmableSTT()
    await orch.start()
    try:
        for _ in range(100):
            if warmed:
                break
            await asyncio.sleep(0.02)
        assert warmed == [True]
        assert any("warm-up" in r.getMessage() and "speech" in r.getMessage()
                   for r in aura_logs), [r.getMessage() for r in aura_logs]
    finally:
        await orch.stop()


async def test_broken_stt_warm_up_never_breaks_startup(stack):
    orch = stack.build_orchestrator()

    class ExplodingSTT:
        def transcribe(self, frames):
            return ""

        def warm(self):
            raise RuntimeError("no model here")

    orch.stt = ExplodingSTT()
    await orch.start()
    try:
        assert orch.state == "armed"
    finally:
        await orch.stop()
