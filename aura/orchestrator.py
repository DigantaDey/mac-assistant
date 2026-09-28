"""The orchestrator — Aura's state machine and the only place that knows
the whole flow:

    ARMED ──wake──► CAPTURING ──► TRANSCRIBING ──► PLANNING ──► GATING
      ▲                                                              │
      │            ┌─────────── confirm / correct / cancel ◄─────────┤
      │            ▼                                                  ▼
      └──RESPONDING ◄──────────── EXECUTING ────────────── PROPOSING
                                                    (TTS + timeline)

Design rules:
* One session at a time. A voice assistant that interleaves sessions is a bug.
* Every stage publishes an event — the UI, the log, and the tests all watch
  the same stream.
* The Laya gate + SafetyGate decide *before* anything executes. Confirmation
  is a real state, not a synchronous prompt: the turn ends, the proposal
  waits (with a timeout), and any client can resolve it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

from .audio import AudioFrame, MicStream, SilentMic
from .events import EventBus
from .laya import ExampleBuffer
from .memory import Memory
from .planner import Action, Planner
from .safety import SafetyGate
from .skills import MacBridge, SkillRegistry
from .stt import STTEngine
from .tts import TTS
from .vad import EnergyVAD
from .wakeword import WakeEngine

State = Literal["armed", "capturing", "transcribing", "planning",
                "proposing", "executing", "responding", "disabled"]


@dataclass
class Session:
    id: str
    transcript: str = ""
    plan: Any = None
    proposal_token: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)  # {action, verdict}
    started: float = field(default_factory=time.monotonic)


class Orchestrator:
    def __init__(
        self,
        cfg,                       # aura.config.Config
        bus: EventBus,
        registry: SkillRegistry,
        bridge: MacBridge,
        planner: Planner,
        laya_backend,              # aura.laya.LayaBackend
        safety: SafetyGate,
        memory: Memory,
        examples: ExampleBuffer,
        stt: STTEngine,
        tts: TTS,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.registry = registry
        self.bridge = bridge
        self.planner = planner
        self.laya = laya_backend
        self.safety = safety
        self.memory = memory
        self.examples = examples
        self.stt = stt
        self.tts = tts

        self.state: State = "armed"
        self.session: Session | None = None
        self._confirmations: dict[str, asyncio.Future[str]] = {}

        self._wake: WakeEngine | None = None
        self._vad = EnergyVAD(end_silence_seconds=cfg.session.end_of_speech_seconds,
                              max_seconds=cfg.session.max_utterance_seconds)
        self._mic: MicStream | SilentMic | None = None
        self._has_audio = False
        self._audio_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=200)
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The orchestrator's loop — set in start(), or lazily when sessions
        are driven directly inside a running loop (tests, typed input)."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        from .wakeword import build_wake_engine

        self._loop = asyncio.get_running_loop()
        self._wake = build_wake_engine(self.cfg)
        try:
            self._mic = MicStream(self._loop, self._on_audio_frame)
            self._mic.start()
            mic_kind = "microphone"
        except Exception as exc:
            self._mic = SilentMic(self._loop, self._on_audio_frame)
            mic_kind = f"silent ({exc})"
        self._audio_task = asyncio.create_task(self._audio_loop(), name="aura-audio")
        self.state = "armed"
        self.bus.publish("state", state=self.state, mic=mic_kind,
                         wake=getattr(self.cfg.wake, "mode", "manual"))
        self.bus.publish("log", line=f"Aura ready — bridge={self.bridge.platform}, "
                                     f"stt={type(self.stt).__name__}, "
                                     f"laya={type(self.laya).__name__}")

    async def stop(self) -> None:
        if self._audio_task:
            self._audio_task.cancel()
            try:
                await self._audio_task
            except (asyncio.CancelledError, RuntimeError):
                pass
            self._audio_task = None
        if self._mic:
            self._mic.stop()
        self.state = "disabled"
        self.bus.publish("state", state=self.state)

    # ------------------------------------------------------------------ #
    # Audio path (real mic only)                                          #
    # ------------------------------------------------------------------ #

    def _on_audio_frame(self, frame: AudioFrame) -> None:
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass  # under load we drop audio, never stall the system

    async def _audio_loop(self) -> None:
        while True:
            frame = await self._queue.get()
            if self.state == "armed" and self._wake:
                if self._wake.feed(frame):
                    await self.begin_capture()
            elif self.state == "capturing":
                verdict = self._vad.feed(frame)
                if verdict in ("end", "timeout"):
                    frames = self._vad.pcm_frames()
                    await self.run_session_frames(frames)

    # ------------------------------------------------------------------ #
    # Session entry points                                                #
    # ------------------------------------------------------------------ #

    async def trigger_manual(self) -> None:
        """Wake from UI/hotkey: start listening for an utterance."""
        if self.state != "armed":
            self.bus.publish("hint", text="I'm already listening — one thing at a time.")
            return
        if not self._has_audio:
            self.bus.publish("hint", text="No microphone here — type your command below.")
            return
        if self._wake is not None and hasattr(self._wake, "fire"):
            self._wake.fire()  # ManualTrigger path
        await self.begin_capture()

    async def begin_capture(self) -> None:
        self.state = "capturing"
        self._vad.reset()
        self.bus.publish("state", state=self.state)
        self.bus.publish("hint", text="Listening…")

    async def submit_text(self, text: str) -> None:
        """Typed input — first-class, not a fallback (privacy mode, no mic, tests)."""
        text = text.strip()
        if not text:
            return
        if self.state not in ("armed",):
            self.bus.publish("hint", text="Hold on — I'm still working on the last request.")
            return
        await self._session_text(text)

    async def run_session_frames(self, frames: list[AudioFrame]) -> None:
        """STT the captured utterance, then run the shared session flow."""
        self.state = "transcribing"
        self.bus.publish("state", state=self.state)
        pcm = b"".join(f.pcm.tobytes() for f in frames if hasattr(f.pcm, "tobytes"))
        if not pcm:
            await self._end_session("I didn't catch anything — say that again?")
            return
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, self.stt.transcribe, [type("F", (), {"pcm": pcm})()]) \
            if False else await loop.run_in_executor(None, self.stt.transcribe,
                                                     [_RawFrame(pcm)])
        text = (text or "").strip()
        if not text:
            await self._end_session("I didn't catch that — say it again?")
            return
        self.bus.publish("transcript", text=text)
        await self._session_text(text, spoken=True)

    # ------------------------------------------------------------------ #
    # The session itself                                                  #
    # ------------------------------------------------------------------ #

    async def _session_text(self, transcript: str, spoken: bool = False) -> None:
        session = Session(id=uuid.uuid4().hex[:12], transcript=transcript)
        self.session = session
        t0 = time.monotonic()

        # 1 — plan
        self.state = "planning"
        self.bus.publish("state", state=self.state, session=session.id)
        context = {
            "recent_turns": [{"role": "user", "content": transcript}],
            "preferences": self.memory.all_preferences(),
        }
        plan = await self.planner.plan(transcript, context)
        session.plan = plan
        self.bus.publish("plan", **plan.as_dict(), session=session.id)

        if not plan.actions:
            await self._respond(session, plan.reply or "Done.", outcome="ok",
                                total_ms=_ms(t0))
            return

        # 2 — gate every action (Laya + safety)
        known = self.registry.names()
        self.state = "proposing"
        for action in plan.actions:
            verdict = self.safety.assess(action, transcript, known)
            session.pending.append({"action": action, "verdict": verdict,
                                    "decision": self.laya.decide(transcript, action.skill,
                                                                 action.args, action.why)})
        needs_confirm = any(p["verdict"].decision == "confirm" for p in session.pending)
        blocked = [p for p in session.pending if p["verdict"].decision == "blocked"]

        if self.cfg.safety.show_plan_before_run and needs_confirm:
            token = uuid.uuid4().hex[:8]
            session.proposal_token = token
            fut: asyncio.Future[str] = self.loop.create_future()
            self._confirmations[token] = fut
            self.bus.publish("proposal", token=token, session=session.id,
                             reply=plan.reply,
                             actions=[{
                                 "skill": p["action"].skill,
                                 "args": p["action"].args,
                                 "risk": p["action"].risk,
                                 "why": p["action"].why,
                                 "verdict": p["verdict"].decision,
                                 "reasons": p["verdict"].reasons,
                             } for p in session.pending])
            self.bus.publish("state", state="proposing")
            try:
                answer = await asyncio.wait_for(
                    fut, timeout=self.cfg.session.confirmation_timeout_seconds)
            except asyncio.TimeoutError:
                answer = "timeout"
            if answer != "confirm":
                for p in session.pending:
                    self._record_example(transcript, p["action"],
                                         "cancelled" if answer in ("cancel", "timeout") else "corrected")
                self.memory.record_event(
                    transcript, plan.as_dict(), plan.reply,
                    outcome="cancelled", total_ms=_ms(t0))
                await self._end_session("No problem — cancelled." if answer == "cancel"
                                        else "I didn't hear a yes, so I cancelled it.")
                return
            # Confirmed: record positive supervision.
            for p in session.pending:
                self._record_example(transcript, p["action"], "confirmed")

        for p in blocked:
            p["result"] = _SkillOutcome(False, f"Refused: {'; '.join(p['verdict'].reasons)}")

        # 3 — execute
        self.state = "executing"
        self.bus.publish("state", state=self.state, session=session.id)
        reply_bits: list[str] = []
        for i, p in enumerate(session.pending):
            if "result" in p:
                continue
            action = p["action"]
            skill = self.registry.get(action.skill)
            self.bus.publish("action_started", index=i, skill=action.skill,
                             session=session.id)
            try:
                result = await skill.execute(action.args, _SkillCtx(self))
            except Exception as exc:  # a crashing skill must never kill the session
                result = _skill_result(False, f"{action.skill} failed: {exc}")
            p["result"] = _SkillOutcome(result.ok, result.message, result.data)
            self.bus.publish("action_result", index=i, skill=action.skill,
                             ok=result.ok, message=result.message, session=session.id)
            if not result.ok:
                reply_bits.append(result.message)

        outcome = "ok" if all(p["result"].ok for p in session.pending
                              if "result" in p) else "failed"
        reply = plan.reply
        if reply_bits:
            reply = (reply + " " if reply else "") + " ".join(reply_bits)
        for p in session.pending:
            if "result" in p and p["verdict"].decision == "run":
                self._record_example(transcript, p["action"], "auto")
        await self._respond(session, reply, outcome=outcome, total_ms=_ms(t0))

    async def _respond(self, session: Session, reply: str, outcome: str, total_ms: int) -> None:
        self.state = "responding"
        self.bus.publish("state", state=self.state, session=session.id)
        self.bus.publish("reply", text=reply, session=session.id,
                         total_ms=total_ms, outcome=outcome)
        self.memory.record_event(session.transcript,
                                 (session.plan.as_dict() if session.plan else {}),
                                 reply, outcome, total_ms)
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.tts.speak, reply)
        except Exception:
            pass
        await self._end_session()

    async def _end_session(self, message: str | None = None) -> None:
        if message:
            self.bus.publish("reply", text=message, total_ms=0, outcome="ok")
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.tts.speak, message)
            except Exception:
                pass
        self.session = None
        self.state = "armed"
        self.bus.publish("state", state=self.state)

    # ------------------------------------------------------------------ #
    # Confirmation / feedback API (called from server threads)             #
    # ------------------------------------------------------------------ #

    def resolve_confirmation(self, token: str, answer: Literal["confirm", "cancel"]) -> bool:
        fut = self._confirmations.get(token)
        if fut and not fut.done():
            fut.set_result(answer)
            return True
        return False

    def record_feedback(self, transcript: str, skill: str, verdict: str, note: str = "") -> None:
        """Explicit 👍/👎 from the Activity timeline — the richest signal we get."""
        self._record_example(transcript, _ShadowAction(skill), verdict)
        if note:
            key, value = self.memory.extract_preference(note)
            self.memory.set_preference(key, value, source="user")
        self.bus.publish("feedback", skill=skill, verdict=verdict)

    # ------------------------------------------------------------------ #

    def _record_example(self, transcript: str, action: Action | "_ShadowAction", outcome: str) -> None:
        if isinstance(action, Action):
            skill, args = action.skill, action.args
        else:
            skill, args = action.skill, {}
        decision = self.laya.decide(transcript, skill, args, "")
        if outcome in ("auto", "confirmed"):
            match, destructive = 1.0, decision.destructive
        elif outcome == "corrected":
            match, destructive = 0.0, decision.destructive
        else:  # cancelled
            match, destructive = 0.0, 1.0
        weight = 2.0 if outcome in ("corrected", "cancelled") else 1.0
        self.examples.record(transcript, skill, args, outcome, match, destructive, weight)


# --------------------------------------------------------------------------- #
# Small internal              #
# --------------------------------------------------------------------------- #


class _RawFrame:
    """Minimal frame wrapper for executor-bound STT calls."""

    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm


class _SkillCtx:
    __slots__ = ("bridge", "memory", "config")

    def __init__(self, orch: "Orchestrator") -> None:
        self.bridge = orch.bridge
        self.memory = orch.memory
        self.config = orch.cfg


class _SkillOutcome:
    __slots__ = ("ok", "message", "data")

    def __init__(self, ok: bool, message: str, data: dict | None = None) -> None:
        self.ok, self.message, self.data = ok, message, data or {}


def _skill_result(ok: bool, message: str, data: dict | None = None):
    from .skills.base import SkillResult
    return SkillResult(ok, message, data or {})


class _ShadowAction:
    """Feedback arrives per skill name; that's enough supervision for the buffer."""

    def __init__(self, skill: str) -> None:
        self.skill = skill
        self.args = {}
        self.why = ""


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
