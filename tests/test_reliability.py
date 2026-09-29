"""The two failures users actually reported, locked down as tests.

1. "I typed 'open youtube' and it's been thinking for ten minutes."
   A session torn down mid-flight (the server used to cancel every
   fire-and-forget request after 30 s) skipped the session's cleanup, so the
   state machine stayed in "planning"/"proposing" forever and every later
   command was refused. A session must *always* come back to "armed".

2. "Record a sample doesn't do anything."
   The VAD threw away the audio it had just reported, so no sample was ever
   captured and the Wake Phrase Studio could never train. These tests drive
   the real audio loop — the path the missing microphone used to break.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import DemoStack, collect

np = pytest.importorskip("numpy")


from aura.audio import AudioFrame
from aura.planner import Action, Plan
from aura.skills.base import SkillResult, SkillSpec

SR = 16_000
FRAME = 512


def _frames(pcm, chunk: int = FRAME) -> list[AudioFrame]:
    now = time.monotonic()
    return [AudioFrame(pcm=pcm[i:i + chunk], ts=now + i / SR)
            for i in range(0, len(pcm) - chunk, chunk)]


def _phrase(seconds: float = 1.0, amp: int = 4000) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    x = (amp * np.sin(2 * np.pi * 190 * t)).astype(np.int16)
    return (x * (np.sin(2 * np.pi * 4 * t) > 0)).astype(np.int16)


def _silence(seconds: float = 1.0) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.int16)


class _SlowPlanner:
    """A local model that answers… eventually."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def plan(self, transcript, context):
        await asyncio.sleep(self.seconds)
        from aura.planner import Action, Plan

        return Plan(reply="Opened it.", actions=[Action("system.open_app", {"app": "X"})])


class _HangingSkill:
    """A skill that never returns — the worst case the watchdog exists for."""

    spec = SkillSpec(name="system.hang", description="never returns")

    async def execute(self, args, ctx):
        await asyncio.sleep(30)          # far longer than the watchdog budget
        return SkillResult(True, "unreachable")


# --------------------------------------------------------------------------- #
# 0 — the first command must not pay for a cold model                          #
# --------------------------------------------------------------------------- #


async def test_engine_warms_the_planner_before_the_first_command(stack: DemoStack):
    """'Still thinking' after install was also the cold local model. The
    engine now loads it in the background at start, best-effort."""
    warmups = []

    class WarmPlanner:
        def warmup(self):
            warmups.append("warmed")
            return True

        async def plan(self, transcript, context):
            return Plan(reply="Opened it.",
                        actions=[Action("system.open_app", {"app": "X"})])

    stack.planner = WarmPlanner()
    orch = stack.build_orchestrator()
    await orch.start()
    try:
        for _ in range(100):                       # it happens on a worker thread
            if warmups:
                break
            await asyncio.sleep(0.02)
        assert warmups == ["warmed"]
        assert orch.state == "armed"               # and the engine still starts
    finally:
        await orch.stop()


async def test_a_planner_without_warmup_does_not_break_startup(stack: DemoStack):
    """The warm-up is an optimisation, never a new hard dependency."""
    orch = stack.build_orchestrator()
    assert not hasattr(orch.planner, "warmup")
    await orch.start()
    try:
        assert orch.state == "armed"
    finally:
        await orch.stop()


# --------------------------------------------------------------------------- #
# 1 — a session must always end, whatever happens to it                        #
# --------------------------------------------------------------------------- #


async def test_cancelled_session_still_returns_to_armed(stack: DemoStack):
    """The regression: `CancelledError` is a BaseException, so the old
    `except Exception` never ran and the orb thought forever."""
    orch = stack.build_orchestrator()
    fast = orch.planner
    orch.planner = _SlowPlanner(seconds=0.4)     # still thinking when cut down
    await orch.start()
    try:
        # exactly what the server used to do: tear the session down mid-flight
        await asyncio.wait_for(orch.submit_text("open spotify"), timeout=0.05)
    except (TimeoutError, asyncio.CancelledError):
        pass
    assert orch.state == "armed", "a cancelled session must not wedge the orb"
    assert orch.session is None
    # and Aura is usable again immediately
    orch.planner = fast
    sid = orch.bus.subscribe_async()
    types = await collect(orch.bus, sid, orch.submit_text("set volume to 30"))
    assert "reply" in types
    assert orch.state == "armed"
    orch.bus.unsubscribe_async(sid)
    await orch.stop()


async def test_cancelled_session_tells_the_user(stack: DemoStack):
    """Being cancelled is not silence: the user gets a sentence and Ready."""
    orch = stack.build_orchestrator()
    orch.planner = _SlowPlanner(seconds=0.4)
    await orch.start()
    sid = orch.bus.subscribe_async()
    replies: list[str] = []

    async def watch():
        while True:
            ev = await orch.bus.get(sid)
            if ev.type == "reply":
                replies.append(ev.data.get("text", ""))

    watcher = asyncio.create_task(watch())
    task = asyncio.create_task(orch.submit_text("open spotify"))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.05)
    watcher.cancel()
    assert replies, "a cancelled session must still answer the user"
    assert orch.state == "armed"
    orch.bus.unsubscribe_async(sid)
    await orch.stop()


async def test_confirmations_are_released_when_a_session_ends(stack: DemoStack):
    """Proposal futures must not outlive their session (they used to leak)."""
    orch = stack.build_orchestrator()
    await orch.start()
    await orch._end_session()
    assert orch._confirmations == {}
    await orch.stop()


async def test_slow_planner_never_wedges_the_session(stack: DemoStack):
    """A model slower than the old 30 s server cap: the session still runs to
    the end and the next command is accepted."""
    orch = stack.build_orchestrator()
    orch.planner = _SlowPlanner(seconds=0.2)
    orch.cfg.session.max_session_seconds = 300.0
    await orch.start()
    sid = orch.bus.subscribe_async()
    types = await collect(orch.bus, sid, orch.submit_text("open spotify"))
    assert "reply" in types
    assert orch.state == "armed"
    orch.bus.unsubscribe_async(sid)
    await orch.stop()


class _HangingPlanner:
    """Plans one action whose skill never returns."""

    async def plan(self, transcript, context):
        from aura.planner import Action, Plan

        return Plan(reply="Working on it.",
                    actions=[Action("system.hang", {}, "safe", "never returns")])


async def test_session_watchdog_ends_a_hung_session(stack: DemoStack):
    """Nothing may think forever: the watchdog ends the session honestly."""
    orch = stack.build_orchestrator()
    orch.planner = _HangingPlanner()
    orch.cfg.session.max_session_seconds = 0.2      # trip it immediately
    orch.cfg.session.confirmation_timeout_seconds = 0.1
    await orch.start()

    from aura.skills.base import SkillRegistry

    registry = SkillRegistry()
    registry.register(_HangingSkill())
    orch.registry = registry

    sid = orch.bus.subscribe_async()
    replies: list[str] = []

    async def watch():
        while True:
            ev = await orch.bus.get(sid)
            if ev.type == "reply":
                replies.append(ev.data.get("text", ""))

    watcher = asyncio.create_task(watch())
    task = asyncio.create_task(orch.submit_text("hang forever"))
    await asyncio.sleep(orch._session_budget_seconds() + 0.4)
    watcher.cancel()
    assert orch.state == "armed", "the watchdog must bring Aura back"
    assert orch.session is None
    assert replies and "too long" in replies[-1], replies
    task.cancel()
    await orch.stop()


# --------------------------------------------------------------------------- #
# 2 — the audio path: what the user actually says                              #
# --------------------------------------------------------------------------- #


class _FakeSTT:
    def transcribe(self, frames):
        pcm = b"".join(f.pcm.tobytes() if hasattr(f.pcm, "tobytes") else bytes(f.pcm)
                       for f in frames)
        return "open spotify" if pcm else ""


async def test_voice_command_runs_end_to_end(stack: DemoStack):
    """Orb click → capture → transcribe → plan → execute → armed.

    This is the flow the empty-capture bug turned into
    "I didn't catch anything — say that again?" for every single utterance.
    """
    orch = stack.build_orchestrator()
    orch.stt = _FakeSTT()
    orch._has_audio = True
    await orch.start()
    sid = orch.bus.subscribe_async()

    await orch.trigger_manual()
    assert orch.state == "capturing"

    for frame in _frames(_phrase(1.2)):
        orch._on_audio_frame(frame)
    for frame in _frames(_silence(1.0)):
        orch._on_audio_frame(frame)

    types = await collect(orch.bus, sid, asyncio.sleep(0.1))
    for _ in range(50):                      # let the session finish
        if orch.state == "armed" and orch.memory.recent_events():
            break
        await asyncio.sleep(0.05)
    types += [t for t in ("state", "reply") if t not in types]

    assert orch.state == "armed"
    assert any("spotify" in c[1].lower() for c in orch.bridge.calls)
    assert orch.memory.recent_events()[0]["transcript"] == "open spotify"
    assert orch.memory.recent_events()[0]["outcome"] == "ok"
    await orch.stop()


async def test_no_second_capture_after_a_voice_session(stack: DemoStack):
    """The stale manual-trigger flag used to re-arm the microphone by itself
    right after a session, so Aura listened again unprompted."""
    orch = stack.build_orchestrator()
    orch.stt = _FakeSTT()
    orch._has_audio = True
    await orch.start()

    await orch.trigger_manual()
    for frame in _frames(_phrase(1.0)) + _frames(_silence(1.0)):
        orch._on_audio_frame(frame)
    for _ in range(50):
        if orch.state == "armed" and orch.memory.recent_events():
            break
        await asyncio.sleep(0.05)
    assert orch.state == "armed"

    # leftover frames from the same burst must not start a new capture
    for frame in _frames(_silence(0.5)):
        orch._on_audio_frame(frame)
    await asyncio.sleep(0.2)
    assert orch.state == "armed", "Aura re-listened on its own"
    await orch.stop()


async def test_event_loop_survives_a_blocking_skill(stack: DemoStack):
    """Skills must not run on the event loop: `osascript` can block for its
    whole timeout while a TCC dialog waits, and that used to freeze the
    engine — SSE, health checks and the next command included."""
    class BlockingBridge:
        platform = "mac"

        def osascript(self, script):
            time.sleep(0.4)                # a TCC dialog the user hasn't answered
            return True, "ok"

        def run(self, argv, timeout=30):
            return True, "ok"

        def open_url(self, url):
            return True, "ok"

        def keystroke(self, text):
            return True, "ok"

    orch = stack.build_orchestrator()
    orch.bridge = BlockingBridge()
    await orch.start()

    heartbeat = []

    async def pulse():
        while True:
            await asyncio.sleep(0.05)
            heartbeat.append(time.monotonic())

    pinger = asyncio.create_task(pulse())
    await collect(orch.bus, orch.bus.subscribe_async(),
                  orch.submit_text("open spotify"))
    pinger.cancel()
    # a responsive loop keeps ticking through the blocking skill
    assert len(heartbeat) > 3, "the event loop was frozen by a blocking skill"
    assert orch.state == "armed"
    await orch.stop()


# --------------------------------------------------------------------------- #
# 3 — the Wake Phrase Studio, through the real audio loop                      #
# --------------------------------------------------------------------------- #


async def test_wake_studio_records_samples_through_the_audio_loop(stack: DemoStack):
    """"Record a sample" must actually capture audio.

    The bug: the training VAD cleared the frames it had just reported, so
    every take was judged "too short", the counter never moved and the
    Train button stayed disabled forever.
    """
    orch = stack.build_orchestrator()
    orch._has_audio = True
    await orch.start()

    assert orch.training_start("Hey Aura")["ok"] is True
    assert orch.training_capture()["ok"] is True

    for frame in _frames(_phrase(1.1)) + _frames(_silence(0.9)):
        orch._on_audio_frame(frame)
    await asyncio.sleep(0.2)

    status = orch.training_status()
    assert status["count"] == 1, f"no sample captured: {status}"
    await orch.stop()


async def test_wake_studio_full_flow_goes_live(stack: DemoStack):
    """Six takes, then Train: a model on disk and a live wake engine."""
    orch = stack.build_orchestrator()
    orch._has_audio = True
    await orch.start()

    assert orch.training_start("Hey Aura")["ok"] is True
    for take in range(6):
        assert orch.training_capture()["ok"] is True
        for frame in _frames(_phrase(1.0 + 0.05 * take)) + _frames(_silence(0.9)):
            orch._on_audio_frame(frame)
        await asyncio.sleep(0.15)
        assert orch.training_status()["count"] == take + 1

    done = await orch.training_finish()
    assert done["ok"], done
    assert done["engine"] == "TemplateWakeEngine"

    from pathlib import Path

    assert Path(done["path"]).is_file()
    assert orch.cfg.wake.mode == "openwakeword"
    assert orch.cfg.wake.phrase == "hey aura"
    await orch.stop()
