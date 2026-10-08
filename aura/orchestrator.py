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
* Lightweight is enforced, not promised: a maintenance loop unloads warm
  models after idle time and hot-applies safe config changes without a
  restart.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from . import config as config_mod
from . import log as log_mod
from .audio import AudioFrame, MicStream, SilentMic
from .events import EventBus
from .laya import ExampleBuffer, HeuristicBackend, LayaBackend
from .log import get_logger, log_exception
from .memory import Memory
from .planner import Action, Planner
from .safety import SafetyGate
from .skills import MacBridge, SkillRegistry
from .stt import STTEngine
from .tts import TTS
from .vad import EnergyVAD
from .wakeword import WakeEngine, phrase_gate, strip_phrase

log = get_logger("orchestrator")

State = Literal["armed", "capturing", "transcribing", "planning",
                "proposing", "executing", "responding", "disabled"]

# Non-negotiable product SLO: active planning/execution ends with either a
# result or an honest timeout. Waiting for the user to approve a risky
# proposal is deliberately separate. Laya is fast (tens of ms warm), but a
# cold checkpoint load or a slow machine gets real headroom — the point of the
# cap is that Aura ALWAYS answers or fails clearly, never hangs.
MAX_ACTIVE_REQUEST_SECONDS = 20.0
TRAIN_CAPTURE_TIMEOUT_SECONDS = 5.0


@dataclass
class Session:
    id: str
    transcript: str = ""
    plan: Any = None
    proposal_token: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)  # {action, verdict}
    started: float = field(default_factory=time.monotonic)


@dataclass
class TrainingState:
    phrase: str
    samples: list = field(default_factory=list)
    message: str = ""


class Orchestrator:
    def __init__(
        self,
        cfg,                       # aura.config.Config
        bus: EventBus,
        registry: SkillRegistry,
        bridge: MacBridge,
        planner: Planner,
        laya_backend: LayaBackend,  # aura.laya.LayaGate (real + offline fallback)
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
        self._wake_error = ""
        self._vad = EnergyVAD(end_silence_seconds=cfg.session.end_of_speech_seconds,
                              max_seconds=cfg.session.max_utterance_seconds)
        self._mic: MicStream | SilentMic | None = None
        self._has_audio = False
        self._audio_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._active_session_task: asyncio.Task | None = None
        self._deadline_expired = False
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=200)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_activity = time.monotonic()
        self.planner_online: bool | None = None   # last probe of the decision backend
        self.planner_status: dict = {}
        self._file_mtimes: dict[str, float] = {}
        self._trainer: TrainingState | None = None
        self._train_capture_armed = False
        self._train_capture_task: asyncio.Task | None = None
        self._train_capture_id = 0
        self._train_vad = EnergyVAD(end_silence_seconds=0.6, max_seconds=4.0)
        self._setup_running = False
        self._laya_idle_unloaded = False

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
        self._loop = asyncio.get_running_loop()
        self.bus.attach_loop(self._loop)
        # Anything the engine itself failed to catch lands in the log file
        # instead of a stderr nobody is reading.
        log_mod.install_exception_hooks(self._loop)
        self._wake = self._build_wake()
        try:
            self._mic = MicStream(self._loop, self._on_audio_frame)
            self._mic.start()
            mic_kind = "microphone"
            self._has_audio = True
        except Exception as exc:
            self._mic = SilentMic(self._loop, self._on_audio_frame)
            mic_kind = f"silent ({exc})"
        self._audio_task = asyncio.create_task(self._audio_loop(), name="aura-audio")
        self._maintenance_task = asyncio.create_task(self._maintenance_loop(),
                                                     name="aura-maintenance")
        self._remember_mtimes()
        await self._probe_planner()
        # Warm the decision model in the background: a cold checkpoint load
        # takes seconds, and paying that on the user's *first* command is
        # exactly what "still thinking" feels like. Best-effort — never fatal.
        asyncio.create_task(self._warm_planner(), name="aura-warmup")
        self.state = "armed"
        self.bus.publish("state", state=self.state, mic=mic_kind,
                         wake=getattr(self.cfg.wake, "mode", "manual"))
        log.info("ready — bridge=%s, mic=%s, stt=%s, tts=%s, planner=%s, "
                 "laya=%s, profile=%s, data=%s",
                 self.bridge.platform, mic_kind, type(self.stt).__name__,
                 type(self.tts).__name__, type(self.planner).__name__,
                 type(self.laya).__name__, config_mod.resolved_profile(self.cfg),
                 self.cfg.data_dir)
        self.bus.publish("log", line=f"Aura ready — bridge={self.bridge.platform}, "
                                     f"stt={type(self.stt).__name__}, "
                                     f"laya={type(self.laya).__name__}, "
                                     f"planner={type(self.planner).__name__}")

    async def _warm_planner(self) -> None:
        """Load the decision model before the user asks for anything.

        A cold Laya checkpoint download/load costs seconds; paying that on the
        first command is exactly the "it's still thinking" feeling. Best-effort
        and off the loop — a machine without the model simply runs the offline
        scorer, and says so in the log.
        """
        warm = getattr(self.planner, "warmup", None)
        if warm is None or not callable(warm):
            warm = getattr(self.laya, "warmup", None)
        if warm is None or not callable(warm):
            return
        try:
            ok = await asyncio.get_running_loop().run_in_executor(None, warm)
            log.info("warm-up: %s", "ready" if ok else "not available (see the log above)")
        except Exception as exc:
            log_exception("warm-up failed", exc, logger=log)

    async def ensure_microphone(self) -> tuple[bool, str]:
        """Attach the live stream after a permission grant without a restart.

        First launch commonly starts the engine before the user answers the
        native TCC dialog. The old process permanently kept its SilentMic, so
        Settings could report a grant while Record Sample still received no
        frames. This method runs on the engine loop and swaps the stream live.
        """
        if self._has_audio and isinstance(self._mic, MicStream):
            return True, "Microphone is already live."
        if self.state in ("capturing", "transcribing"):
            return False, "Finish the current recording, then try again."
        if self._mic is not None:
            self._mic.stop()
        try:
            mic = MicStream(self.loop, self._on_audio_frame)
            mic.start()
        except Exception as exc:
            self._mic = SilentMic(self.loop, self._on_audio_frame)
            self._has_audio = False
            self.bus.publish("log", line=f"microphone reconnect failed: {exc}")
            return False, f"Microphone permission is set, but the input could not open: {exc}"
        self._mic = mic
        self._has_audio = True
        self.bus.publish("microphone", ready=True)
        self.bus.publish("log", line="microphone attached after permission grant")
        return True, "Microphone is ready — recording and wake phrases are live."

    async def stop(self) -> None:
        tasks = {task for task in (
            self._audio_task, self._maintenance_task, self._watchdog_task,
            self._train_capture_task, self._active_session_task,
        ) if task is not None and task is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, RuntimeError):
                pass
        self._audio_task = None
        self._maintenance_task = None
        self._watchdog_task = None
        self._train_capture_task = None
        self._active_session_task = None
        if self._mic:
            self._mic.stop()
        self.state = "disabled"
        self.bus.publish("state", state=self.state)

    # ------------------------------------------------------------------ #
    # Maintenance: hot-reload + idle unload                               #
    # ------------------------------------------------------------------ #

    def _remember_mtimes(self) -> None:
        self._file_mtimes = {}
        for path in config_mod.watch_paths(self.cfg.data_dir):
            try:
                self._file_mtimes[str(path)] = path.stat().st_mtime
            except OSError:
                continue

    async def _maintenance_loop(self) -> None:
        """Every 10 s: apply live config changes, unload idle models, probe
        the planner so the UI can tell the user when the brain is offline."""
        while True:
            await asyncio.sleep(10.0)
            self._poll_config_changes()
            self._unload_if_idle()
            await self._probe_planner()

    async def _probe_planner(self) -> None:
        probe = getattr(self.planner, "probe", None)
        status = getattr(self.planner, "status", None)
        if probe is None:
            # The Laya planner has no HTTP endpoint to poll — its status is the
            # backend's own (ready / loading / the reason it fell back), which
            # the UI shows next to the "running on basics" badge.
            if status is not None:
                try:
                    self.planner_status = status() if callable(status) else dict(status)
                    self.planner_online = bool(self.planner_status.get("ready", False))
                except Exception as exc:
                    log_exception("planner status check failed", exc, logger=log)
                    self.planner_online = False
            else:
                self.planner_online = None        # deterministic planner — nothing to check
            return
        try:
            self.planner_online = bool(await asyncio.get_running_loop()
                                       .run_in_executor(None, probe))
            self.planner_status = status() if callable(status) else dict(status)
        except Exception as exc:
            log_exception("planner probe failed", exc, logger=log)
            self.planner_online = False

    def _poll_config_changes(self) -> None:
        current: dict[str, float] = {}
        for path in config_mod.watch_paths(self.cfg.data_dir):
            try:
                current[str(path)] = path.stat().st_mtime
            except OSError:
                continue
        if current == self._file_mtimes:
            return
        self._file_mtimes = current
        try:
            fresh = config_mod.load_config(data_dir=self.cfg.data_dir)
        except Exception as exc:  # a broken edit must not crash the loop
            self.bus.publish("log", line=f"config reload failed: {exc}")
            return
        self.apply_live_config(fresh)

    def apply_live_config(self, fresh) -> None:
        """Hot-apply only the fields in config.LIVE_FIELDS; report the rest."""
        changed: list[str] = []
        for section_name, allowed in config_mod.LIVE_FIELDS.items():
            old_section = getattr(self.cfg, section_name)
            new_section = getattr(fresh, section_name)
            for field_name in allowed:
                new_value = getattr(new_section, field_name)
                if getattr(old_section, field_name) != new_value:
                    setattr(old_section, field_name, new_value)
                    changed.append(f"{section_name}.{field_name}")
        if not changed:
            return
        if "wake.mode" in changed or "wake.models" in changed or "wake.threshold" in changed:
            self._wake = self._build_wake()
        if "stt.whisper_model" in changed:
            from .stt import build_stt

            self.stt = build_stt(self.cfg)
        self.bus.publish("config", changed=changed)
        self.bus.publish("log", line="hot-reloaded: " + ", ".join(changed))

    def _unload_if_idle(self) -> None:
        idle_for = time.monotonic() - self._last_activity
        if idle_for < self.cfg.session.idle_unload_seconds:
            return
        unload = getattr(self.stt, "unload", None)
        if unload is not None:
            try:
                if unload():
                    self.bus.publish("log",
                                     line=f"STT model unloaded after {int(idle_for)}s idle")
            except Exception as exc:
                log_exception("stt unload failed", exc, logger=log)

        # The decision model is the other large resident. Releasing it is
        # opt-in (laya.idle_unload_seconds > 0) because the next question then
        # pays the reload — an honest trade, not a default.
        budget = float(getattr(self.cfg.laya, "idle_unload_seconds", 0.0) or 0.0)
        if not budget or idle_for < budget or self._laya_idle_unloaded:
            return
        release = getattr(self.laya, "unload", None)
        if not callable(release):
            return
        try:
            if release():
                self._laya_idle_unloaded = True
                log.info("laya: checkpoints released after %ds idle", int(idle_for))
        except Exception as exc:
            log_exception("laya unload failed", exc, logger=log)

    # ------------------------------------------------------------------ #
    # Wake management (live, persisted)                                   #
    # ------------------------------------------------------------------ #

    def _build_wake(self) -> WakeEngine:
        from .wakeword import build_wake_engine

        self._wake_error = ""

        def on_fallback(reason: str) -> None:
            self._wake_error = reason
            self.bus.publish("log", line=f"wake: {reason}")
            self.bus.publish("wake_fallback", reason=reason)

        return build_wake_engine(self.cfg, on_fallback=on_fallback)

    def wake_status(self) -> dict[str, Any]:
        """Report the listener that is actually running, not just the saved toggle."""
        from .wakeword import ManualTrigger

        engine = self._wake
        engine_name = type(engine).__name__ if engine is not None else "Unavailable"
        mode = self.cfg.wake.mode
        active = False
        if mode == "manual":
            detail = "Tap the Aura orb or press the shortcut to start listening."
        elif not self._has_audio:
            detail = ("Always listening is selected, but Aura has no live microphone. "
                      "Allow microphone access, then try again.")
        elif self._audio_task is None or self._audio_task.done():
            detail = "Always listening is selected, but Aura's audio listener is not running. Restart the engine."
        elif engine is None:
            detail = self._wake_error or "Always listening is selected, but no wake detector is available."
        elif isinstance(engine, ManualTrigger):
            detail = self._wake_error or (
                "Always listening is selected, but no wake detector is active. "
                "Install a wake model or train your phrase.")
        else:
            active = engine is not None
            phrase = (self.cfg.wake.phrase.strip()
                      or str(getattr(engine, "phrase", "")).strip())
            if not phrase:
                for model in self.cfg.wake.models or []:
                    path = str(model)
                    if path.endswith(".npz"):
                        continue  # TemplateWakeEngine exposes its phrase directly.
                    if path.rsplit("/", 1)[-1] in {
                        "hey_jarvis", "hey_mycroft", "hey_rhasspy", "alexa", "okay_nabu",
                    }:
                        phrase = path.rsplit("/", 1)[-1].replace("_", " ").title()
                    if phrase:
                        break
            detail = (f"Always listening is active — say “{phrase}”." if phrase
                      else "Always listening is active with the installed wake model.")
        return {"mode": mode, "engine": engine_name, "active": active,
                "error": self._wake_error, "detail": detail}

    async def set_wake_mode(self, mode: str, phrase: str | None = None) -> dict:
        """Switch manual ↔ always-listening at runtime; persist only a live mode."""
        if mode not in ("manual", "openwakeword"):
            return {"ok": False, "message": f"unknown wake mode {mode!r}"}
        if config_mod.resolved_profile(self.cfg) == "demo":
            return {"ok": False,
                    "message": "Demo profile pins manual wake — set profile = \"mac\" "
                               "in config.toml to enable always-listening."}

        old_mode, old_phrase = self.cfg.wake.mode, self.cfg.wake.phrase
        old_wake, old_error = self._wake, self._wake_error
        if mode == "openwakeword" and not self._has_audio:
            return {"ok": False,
                    "mode": old_mode,
                    "message": "Always listening needs a live microphone. Allow Microphone access in Setup, then try again."}
        if mode == "openwakeword" and (
                self._audio_task is None or self._audio_task.done()):
            return {"ok": False, "mode": old_mode,
                    "message": "Aura's audio listener is not running. Restart the engine, then try again."}

        self.cfg.wake.mode = mode
        if phrase is not None:
            self.cfg.wake.phrase = phrase.strip()[:60]
        candidate = self._build_wake()
        from .wakeword import ManualTrigger

        if mode == "openwakeword" and isinstance(candidate, ManualTrigger):
            reason = self._wake_error or "The wake detector could not be started."
            self.cfg.wake.mode, self.cfg.wake.phrase = old_mode, old_phrase
            self._wake, self._wake_error = old_wake, old_error
            return {"ok": False, "mode": old_mode, "engine": type(old_wake).__name__,
                    "message": f"Always listening couldn't start: {reason}"}

        self._wake = candidate
        try:
            config_mod.write_overrides(
                self.cfg.data_dir,
                {"wake": {"mode": mode, "phrase": self.cfg.wake.phrase}},
            )
        except OSError as exc:
            self.bus.publish("log", line=f"could not persist wake mode: {exc}")
        self._remember_mtimes()
        self.bus.publish("config", changed=["wake.mode"], wake_mode=mode,
                         phrase=self.cfg.wake.phrase)
        note = (f"Wake mode: {mode}" +
                (f" (phrase gate: “{self.cfg.wake.phrase}”)" if self.cfg.wake.phrase else ""))
        self.bus.publish("log", line=note)
        status = self.wake_status()
        return {"ok": True, "mode": mode, "phrase": self.cfg.wake.phrase,
                "engine": type(self._wake).__name__, "wake_active": status["active"],
                "wake_detail": status["detail"]}

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
            if self.state == "armed" and self._trainer is not None and self._train_capture_armed:
                verdict = self._train_vad.feed(frame)
                if verdict in ("end", "timeout"):
                    frames = self._train_vad.pcm_frames()
                    self._train_capture_armed = False
                    self._handle_train_sample(frames)
            elif self.state == "armed" and self._wake:
                try:
                    detected = self._wake.feed(frame)
                except Exception as exc:
                    # A detector's first inference can fail after successful
                    # model construction. Keep the audio loop alive, degrade
                    # explicitly to manual wake, and expose the real status.
                    from .wakeword import ManualTrigger

                    reason = f"wake detector failed during audio processing: {exc}"
                    self._wake_error = reason
                    self._wake = ManualTrigger()
                    self.bus.publish("log", line=reason)
                    self.bus.publish("wake_fallback", reason=reason)
                    detected = False
                if detected:
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
        # No `fire()` here: begin_capture() moves straight to "capturing", so
        # the audio loop never reaches `wake.feed()` to consume the flag — it
        # would sit armed and fire a *second*, unwanted capture as soon as the
        # first session ended (Aura re-listening on its own, then "I didn't
        # catch anything" 12 s later).
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
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(None, self.stt.transcribe, [_RawFrame(pcm)]),
                timeout=self._session_budget_seconds(),
            )
        except TimeoutError:
            await self._end_session(
                f"Speech recognition took too long (over "
                f"{self._session_budget_seconds():.0f} seconds). Please try again.",
                outcome="failed")
            return
        except Exception as exc:
            self.bus.publish("log", line=f"speech recognition failed: {exc}")
            await self._end_session(
                "Speech recognition failed — you can type the command or try again.",
                outcome="failed")
            return
        text = (text or "").strip()
        if not text:
            await self._end_session("I didn't catch that — say it again?")
            return

        # Phrase gate: in always-on mode the utterance must start with the
        # user's wake phrase — the second factor against false wakes.
        if (self.cfg.wake.mode == "openwakeword" and self.cfg.wake.phrase
                and not phrase_gate(text, self.cfg.wake.phrase)):
            self.bus.publish("log", line=f"phrase gate rejected: {text[:60]!r}")
            await self._end_session()
            return
        text = strip_phrase(text, self.cfg.wake.phrase) if self.cfg.wake.mode == "openwakeword" else text
        if not text:
            await self._end_session("Listening.")
            return

        self.bus.publish("transcript", text=text)
        await self._session_text(text, spoken=True)

    # ------------------------------------------------------------------ #
    # The session itself                                                  #
    # ------------------------------------------------------------------ #

    async def _session_text(self, transcript: str, spoken: bool = False) -> None:
        """A session must *always* end — with a result or a polite apology.
        An unexpected error is a product bug, not a frozen orb.

        `BaseException` on purpose: `asyncio.CancelledError` is not an
        `Exception` (Python 3.8+), and a session torn down mid-flight used to
        skip this handler entirely — leaving the state machine stuck in
        "planning"/"proposing" forever, with every later command refused and
        the panel saying "Thinking…" for as long as the user cared to wait.
        """
        current = asyncio.current_task()
        self._active_session_task = current
        self._deadline_expired = False
        try:
            await self._run_session(transcript, spoken)
        except asyncio.CancelledError:
            deadline = self._deadline_expired
            log.warning("session: %s — %r", "deadline reached" if deadline else "cancelled",
                        transcript[:60])
            self.bus.publish(
                "log",
                line=("active request deadline reached — back to ready" if deadline
                      else "session cancelled — back to ready"),
            )
            message = (f"That took too long (over {self._session_budget_seconds():.0f} "
                       "seconds), so I stopped it. Please try again."
                       if deadline else "I stopped that one — ask me again?")
            session = self.session
            plan = session.plan.as_dict() if session and session.plan else {}
            await self._end_session(message, outcome="failed")
            if session is not None:
                await self._record_session_event(
                    session.id, session.transcript, plan, message, "failed",
                    _ms(session.started))
        except BaseException as exc:  # the orb must never freeze
            detail = log_exception(f"session failed while handling {transcript[:60]!r}",
                                   exc, logger=log)
            self.bus.publish("log", line=f"session error: {detail}")
            session = self.session
            plan = session.plan.as_dict() if session and session.plan else {}
            message = self._failure_message("Something went wrong while thinking.", detail)
            # The user gets the *detail*, not just an apology: "something went
            # wrong" is unfixable, "LayaError: laya predict: …" is a bug report.
            try:
                await self._end_session(message, outcome="failed", diagnostic=detail)
                if session is not None:
                    await self._record_session_event(
                        session.id, session.transcript, plan, message, "failed",
                        _ms(session.started))
            except BaseException:  # even the apology must not hang the state
                session_id = self.session.id if self.session is not None else None
                self._disarm_watchdog()
                self._release_confirmations()
                self.session = None
                self._active_session_task = None
                self._deadline_expired = False
                self.state = "armed"
                self.bus.publish("state", state=self.state, session=session_id)

    @staticmethod
    def _failure_message(headline: str, detail: str) -> str:
        """A spoken apology that still tells an engineer what broke."""
        if not detail:
            return headline
        short = detail if len(detail) <= 140 else detail[:139] + "…"
        return f"{headline} ({short})"

    async def _decide_actions(self, transcript: str, actions: list[Action]) -> list[Any]:
        """Ask the decision layer about every action of this request, once.

        All actions go to Laya in one call (`predict_batch` when the installed
        version has it), off the event loop, so a two-action command costs one
        model round-trip rather than two. A gate failure is logged with its
        traceback and answered by the offline scorer — never raised into the
        session, because an unjudged action must not run.
        """
        cases = [(action.skill, action.args, action.why) for action in actions]
        started = time.monotonic()
        try:
            loop = asyncio.get_running_loop()
            decisions = list(await loop.run_in_executor(
                None, self.laya.decide_many, transcript, cases))
            if len(decisions) != len(cases):
                raise ValueError(f"gate returned {len(decisions)} decisions for "
                                 f"{len(cases)} actions")
        except Exception as exc:
            detail = log_exception("gate: the Laya decision call failed — every action of "
                                   "this request is judged by the offline scorer", exc,
                                   logger=log)
            fallback = HeuristicBackend()
            decisions = []
            for skill, args, why in cases:
                decision = fallback.decide(transcript, skill, args, why)
                decision.error = detail
                decision.source = "fallback"
                decisions.append(decision)
        elapsed = (time.monotonic() - started) * 1000.0
        log.info("gate: %d action(s) in %.1fms — %s", len(cases), elapsed,
                 ", ".join(f"{d.backend}/{d.source} match={d.match:.2f} "
                           f"destructive={d.destructive:.2f}"
                           + (f" error={d.error}" if d.error else "")
                           for d in decisions))
        return decisions

    async def _run_session(self, transcript: str, spoken: bool = False) -> None:
        session = Session(id=uuid.uuid4().hex[:12], transcript=transcript)
        self.session = session
        self._last_activity = time.monotonic()
        t0 = time.monotonic()
        self._arm_watchdog()
        self._laya_idle_unloaded = False
        log.info("session %s: %r (spoken=%s)", session.id, transcript[:120], spoken)

        # 1 — plan
        self.state = "planning"
        self.bus.publish("state", state=self.state, session=session.id)
        context = {
            "recent_turns": [{"role": "user", "content": transcript}],
            "preferences": self.memory.all_preferences(),
        }
        plan = await self.planner.plan(transcript, context)
        session.plan = plan
        log.info("plan: %s via %s in %dms (model %dms, degraded=%s%s) — %s",
                 [a.skill for a in plan.actions], plan.routed_by or plan.source,
                 plan.latency_ms, plan.model_ms, plan.degraded,
                 f", diagnostic={plan.diagnostic}" if plan.diagnostic else "",
                 plan.reply[:120])
        self.bus.publish("plan", **plan.as_dict(), session=session.id)

        if not plan.actions:
            await self._respond(session, plan.reply or "Done.", outcome="ok",
                                total_ms=_ms(t0))
            return

        # 2 — gate every action (Laya + safety). One batched decision call
        #     covers every action of the request: both questions per action,
        #     answered off the event loop under the gate's own deadline.
        known = self.registry.names()
        # The gate runs while the plan is still "planning": a proposal that has
        # no token yet is not something the user could answer, and `/api/state`
        # must never hand out a `proposing` snapshot the app cannot confirm.
        decisions = await self._decide_actions(transcript, plan.actions)
        for action, decision in zip(plan.actions, decisions):
            verdict = self.safety.assess(action, transcript, known, decision=decision)
            session.pending.append({"action": action, "verdict": verdict,
                                    "decision": decision})
        needs_confirm = any(p["verdict"].decision == "confirm" for p in session.pending)
        blocked = [p for p in session.pending if p["verdict"].decision == "blocked"]

        if needs_confirm or self.cfg.safety.show_plan_before_run:
            # `needs_confirm` is non-negotiable — the safety gate's "ask" can
            # never be switched off by a setting. `show_plan_before_run` is
            # the opt-in *strict* mode: even safe actions pause for a yes.
            token = uuid.uuid4().hex[:8]
            session.proposal_token = token
            self.state = "proposing"
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
            timeout = self.cfg.session.confirmation_timeout_seconds
            log.info("proposal %s: %s — waiting up to %.0fs for your go-ahead",
                     token, [p["action"].skill for p in session.pending], timeout)
            # A voice user isn't looking at a card: the question itself must
            # be spoken ("…go ahead?"), not only the eventual outcome.
            if plan.reply:
                self._speak_async(plan.reply)
            # A proposal is already a response inside the active-work budget.
            # Reading it is user time, not active work, so suspend the active
            # watchdog while the independent confirmation timer runs.
            self._disarm_watchdog()
            try:
                answer = await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError:
                answer = "timeout"
            finally:
                self._confirmations.pop(token, None)
            if answer == "timeout":
                log.info("proposal %s: no answer within %.0fs — cancelled",
                         token, timeout)
            if answer != "confirm":
                log.info("proposal %s: answered %r — nothing will run", token, answer)
                for p in session.pending:
                    self._record_example(transcript, p["action"],
                                         "cancelled" if answer in ("cancel", "timeout") else "corrected",
                                         p.get("decision"))
                message = ("No problem — cancelled." if answer == "cancel"
                           else "I didn't hear a yes, so I cancelled it.")
                await self._end_session(message)
                await self._record_session_event(
                    session.id, transcript, plan.as_dict(), plan.reply,
                    "cancelled", _ms(t0))
                return
            # Confirmed: record positive supervision, then give execution its
            # own active-work window under the watchdog.
            log.info("proposal %s: confirmed — running %d action(s)",
                     token, len(session.pending))
            for p in session.pending:
                self._record_example(transcript, p["action"], "confirmed", p.get("decision"))
            self._arm_watchdog()
            # The question ("…go ahead?") was already asked — spoken and
            # shown — when the proposal went out. The reply that follows
            # execution should report the OUTCOME, not ask again; drop the
            # canned question so `_respond` falls back to the grounded
            # result ("Trash emptied.", "Opened … in Safari.").
            plan.reply = ""

        for p in blocked:
            p["result"] = _SkillOutcome(False, f"Refused: {'; '.join(p['verdict'].reasons)}")

        # 3 — execute (parallel for independent safe actions)
        self.state = "executing"
        self.bus.publish("state", state=self.state, session=session.id)

        # Gather all actionable (non-blocked) skills with their indices.
        executable: list[tuple[int, dict]] = [
            (i, p) for i, p in enumerate(session.pending) if "result" not in p
        ]

        async def _run_one(idx: int, pending: dict) -> None:
            action = pending["action"]
            skill = self.registry.get(action.skill)
            started_at = time.monotonic()
            self.bus.publish("action_started", index=idx, skill=action.skill,
                             session=session.id)
            log.info("run: %s %s", action.skill, action.args)
            try:
                # Skills reach macOS through *blocking* subprocesses, and
                # `osascript` can sit there for its whole timeout while a TCC
                # consent dialog waits for the user. Running that on the event
                # loop froze the whole engine — SSE, health checks, the next
                # command — for as long as macOS took to answer. A worker
                # thread keeps Aura responsive (and the orb breathing) while
                # the skill works.
                result = await asyncio.to_thread(
                    _execute_skill, skill, action.args, _SkillCtx(self))
            except Exception as exc:  # a crashing skill must never kill the session
                result = _skill_result(False, f"{action.skill} failed: {exc}")
            pending["result"] = _SkillOutcome(result.ok, result.message, result.data)
            (log.info if result.ok else log.warning)(
                "result: %s %s in %.0fms — %s", action.skill,
                "ok" if result.ok else "failed", (time.monotonic() - started_at) * 1000.0,
                result.message)
            self.bus.publish("action_result", index=idx, skill=action.skill,
                             ok=result.ok, message=result.message, session=session.id)

        # Run all executable actions concurrently — e.g. "open Spotify and set
        # volume to 30" fires both at once instead of sequentially.
        if executable:
            await asyncio.gather(*[_run_one(i, p) for i, p in executable])

        reply_bits: list[str] = []
        for p in session.pending:
            if "result" in p and not p["result"].ok:
                reply_bits.append(p["result"].message)

        outcome = "ok" if all(p["result"].ok for p in session.pending
                              if "result" in p) else "failed"
        reply = plan.reply
        if reply_bits:
            reply = (reply + " " if reply else "") + " ".join(reply_bits)
        if not reply and session.pending:
            # No canned reply — speak the grounded result of the first action.
            first_ok = next((p["result"].message for p in session.pending
                             if "result" in p and p["result"].ok), None)
            reply = first_ok or (reply_bits[0] if reply_bits else "")
        for p in session.pending:
            if "result" in p and p["verdict"].decision == "run":
                self._record_example(transcript, p["action"], "auto", p.get("decision"))
        await self._respond(session, reply, outcome=outcome, total_ms=_ms(t0))

    async def _record_session_event(self, session_id: str, transcript: str, plan: dict,
                                    reply: str, outcome: str, total_ms: int) -> None:
        """Persist history off-loop, after the user-facing session is ready.

        SQLite is local, but a busy database or a slow disk must not keep the
        orb in Responding after the action and reply are already complete.
        """
        try:
            await asyncio.to_thread(self.memory.record_event, transcript, plan,
                                    reply, outcome, total_ms)
        except Exception as exc:
            detail = log_exception("history: couldn't save the completed session", exc,
                                   logger=log)
            self.bus.publish("log", line=f"activity history save failed: {detail}")
        else:
            self.bus.publish("history_updated", session=session_id)

    async def _respond(self, session: Session, reply: str, outcome: str, total_ms: int,
                       diagnostic: str = "") -> None:
        self.state = "responding"
        self.bus.publish("state", state=self.state, session=session.id)
        plan = session.plan
        plan_data = plan.as_dict() if plan else {}
        log.info("reply (%s, %dms, %s): %s", outcome, total_ms,
                 getattr(plan, "routed_by", "?") or "?", reply[:200])
        self.bus.publish("reply", text=reply, session=session.id,
                         total_ms=total_ms, outcome=outcome,
                         diagnostic=diagnostic or getattr(plan, "diagnostic", ""),
                         degraded=bool(getattr(plan, "degraded", False)))
        # Fire TTS in the background — the user can issue their next command
        # immediately instead of waiting for speech to finish.
        self._speak_async(reply)
        self._last_activity = time.monotonic()
        # Finish the visible state before touching disk. The history write runs
        # in a worker and announces completion separately for Activity.
        await self._end_session()
        await self._record_session_event(session.id, session.transcript, plan_data,
                                         reply, outcome, total_ms)

    def _speak_async(self, text: str) -> None:
        """Speak on a worker without blocking the session or leaking failures."""
        if self._loop is None:
            return
        future = self._loop.run_in_executor(None, self.tts.speak, text)

        def report_failure(done) -> None:
            try:
                done.result()
            except Exception as exc:
                log.warning("text-to-speech failed: %s: %s", type(exc).__name__, exc)

        future.add_done_callback(report_failure)

    async def _end_session(self, message: str | None = None,
                           outcome: str = "ok", diagnostic: str = "") -> None:
        self._disarm_watchdog()
        self._release_confirmations()
        session_id = self.session.id if self.session is not None else None
        if message:
            if diagnostic:
                log.warning("session ended (%s): %s", outcome, diagnostic)
            self.bus.publish("reply", text=message, session=session_id,
                             total_ms=0, outcome=outcome, diagnostic=diagnostic)
            self._speak_async(message)
        self.session = None
        self._active_session_task = None
        self._deadline_expired = False
        self.state = "armed"
        self._last_activity = time.monotonic()
        self.bus.publish("state", state=self.state, session=session_id)

    # ------------------------------------------------------------------ #
    # The session watchdog — the promise that the orb always comes back    #
    # ------------------------------------------------------------------ #

    def _session_budget_seconds(self) -> float:
        """Hard ceiling for active planning/execution.

        The MAX_ACTIVE_REQUEST_SECONDS cap is enforced even when an existing
        runtime.toml still contains the old 300-second default. Confirmation
        waits are suspended from this budget in `_run_session` and use their
        own timer. A small floor keeps malformed zero/negative config useful in
        tests and prevents an accidental immediate cancellation.
        """
        configured = float(getattr(
            self.cfg.session, "max_session_seconds", MAX_ACTIVE_REQUEST_SECONDS))
        return max(0.1, min(configured, MAX_ACTIVE_REQUEST_SECONDS))

    def _arm_watchdog(self) -> None:
        self._disarm_watchdog()
        try:
            # Capture the owner explicitly. Resetting only the public state
            # while this task kept running caused late actions/replies to leak
            # into the next session.
            self._active_session_task = asyncio.current_task()
            self._deadline_expired = False
            self._watchdog_task = asyncio.create_task(self._watchdog(),
                                                      name="aura-watchdog")
        except RuntimeError:            # no running loop (direct-call tests)
            self._watchdog_task = None

    def _disarm_watchdog(self) -> None:
        task, self._watchdog_task = self._watchdog_task, None
        if (task is not None and not task.done()
                and task is not asyncio.current_task()):
            task.cancel()

    async def _watchdog(self) -> None:
        try:
            await asyncio.sleep(self._session_budget_seconds())
        except asyncio.CancelledError:
            return
        if self.session is None and self.state == "armed":
            return                          # the session already ended
        self.bus.publish("log",
                         line=f"active request exceeded {self._session_budget_seconds():.0f}s deadline")
        self._deadline_expired = True
        owner = self._active_session_task
        if owner is not None and not owner.done() and owner is not asyncio.current_task():
            # Cancellation unwinds the actual planner/skill coroutine; its
            # handler records one failure and returns Aura to Ready. Merely
            # resetting state here would let detached work act on the Mac later.
            owner.cancel()
            return
        # Defensive fallback for direct-call integrations with no owner task.
        await self._end_session(
            f"That took too long (over {self._session_budget_seconds():.0f} seconds), "
            "so I stopped it. Please try again.",
            outcome="failed")

    def _release_confirmations(self) -> None:
        """Wake (and forget) any proposal still waiting for an answer."""
        pending, self._confirmations = self._confirmations, {}
        for fut in pending.values():
            if not fut.done():
                try:
                    fut.set_result("timeout")
                except Exception:          # a cancelled future is already done
                    pass

    # ------------------------------------------------------------------ #
    # Wake Phrase Studio — guided, in-app training (no terminal, ever)     #
    # ------------------------------------------------------------------ #

    TRAIN_SAMPLES_NEEDED = 6
    TRAIN_SAMPLES_MINIMUM = 3

    def _cancel_training_capture_timeout(self) -> None:
        """Invalidate and cancel the no-speech timer, from any thread."""
        self._train_capture_id += 1

        def cancel() -> None:
            task, self._train_capture_task = self._train_capture_task, None
            if (task is not None and not task.done()
                    and task is not asyncio.current_task()):
                task.cancel()

        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if self._loop is not None and here is not self._loop:
            self._loop.call_soon_threadsafe(cancel)
        else:
            cancel()

    def _arm_training_capture_timeout(self) -> None:
        """A record tap always resolves, even when no speech reaches the mic."""
        self._cancel_training_capture_timeout()
        capture_id = self._train_capture_id

        async def expire() -> None:
            try:
                await asyncio.sleep(TRAIN_CAPTURE_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                return
            if (capture_id != self._train_capture_id or not self._train_capture_armed
                    or self._trainer is None):
                return
            self._train_capture_armed = False
            self._train_vad.reset()
            count = len(self._trainer.samples)
            message = "I didn't hear a phrase — tap Record and try again."
            self._trainer.message = message
            self.bus.publish("train_sample", ok=False, message=message,
                             count=count, need=self.TRAIN_SAMPLES_NEEDED,
                             phase="capture", seconds=0.0)
            self.bus.publish("train_update", phase="capture",
                             phrase=self._trainer.phrase, count=count,
                             need=self.TRAIN_SAMPLES_NEEDED, message=message)
            self._train_capture_task = None

        def create() -> None:
            if capture_id != self._train_capture_id:
                return
            self._train_capture_task = self.loop.create_task(
                expire(), name="aura-train-capture-timeout")

        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if self._loop is not None and here is not self._loop:
            self._loop.call_soon_threadsafe(create)
        elif here is not None:
            if self._loop is None:
                self._loop = here
            create()
        # With no running engine loop (a synchronous unit test), fake samples
        # are injected directly and there is no wall-clock capture to time out.

    def training_start(self, phrase: str) -> dict:
        phrase = " ".join(str(phrase or "").split())
        words = phrase.split()
        if not (1 <= len(words) <= 5) or not all(w.isalpha() for w in words):
            return {"ok": False,
                    "message": "Pick one to five simple words — they become the phrase you train."}
        if not self._has_audio:
            return {"ok": False,
                    "message": "Aura can't hear you yet — allow the microphone in Setup first."}
        try:
            import numpy  # noqa: F401
        except Exception:
            return {"ok": False,
                    "message": "The trainer needs a component — install it from Setup."}
        if self.state != "armed" or self.session is not None:
            return {"ok": False, "message": "Finish the current request first."}

        self._cancel_training_capture_timeout()
        self._train_capture_armed = False
        self._train_vad.reset()
        ready_message = "Ready — tap Record, then say your phrase."
        self._trainer = TrainingState(phrase=phrase.lower(), message=ready_message)
        self.bus.publish("train_update", phase="capture", phrase=phrase.lower(),
                         count=0, need=self.TRAIN_SAMPLES_NEEDED,
                         message=ready_message)
        return {"ok": True, "need": self.TRAIN_SAMPLES_NEEDED,
                "minimum": self.TRAIN_SAMPLES_MINIMUM}

    def training_capture(self) -> dict:
        if self._trainer is None:
            return {"ok": False, "message": "Start training first."}
        if not self._has_audio:
            return {"ok": False,
                    "message": "Aura's microphone isn't connected — allow it in Settings and try again."}
        if self.state != "armed" or self.session is not None:
            return {"ok": False, "message": "One moment — finish the current request."}
        if self._train_capture_armed:
            return {"ok": False, "message": "Already recording — say your phrase now."}
        if len(self._trainer.samples) >= self.TRAIN_SAMPLES_NEEDED:
            return {"ok": False, "message": "All samples are ready — train the phrase."}
        self._train_capture_armed = True
        self._train_vad.reset()
        self._trainer.message = "Recording — say your phrase now, then pause briefly."
        self._arm_training_capture_timeout()
        self.bus.publish("train_update", phase="listening",
                         phrase=self._trainer.phrase,
                         count=len(self._trainer.samples),
                         need=self.TRAIN_SAMPLES_NEEDED,
                         message=self._trainer.message)
        return {"ok": True, "message": self._trainer.message}

    def _handle_train_sample(self, frames: list[AudioFrame]) -> None:
        self._cancel_training_capture_timeout()
        self._train_capture_armed = False
        if self._trainer is None:
            return
        import numpy as np

        from .wakeword_trainer import judge_sample

        pcm = b"".join(f.pcm.tobytes() for f in frames if hasattr(f.pcm, "tobytes"))
        quality = judge_sample(pcm)
        count = len(self._trainer.samples)
        if quality.ok:
            self._trainer.samples.append(
                np.frombuffer(pcm, dtype=np.int16).astype(np.float32))
            count = len(self._trainer.samples)
            message = (f"Sample {count} of {self.TRAIN_SAMPLES_NEEDED} captured."
                       if count < self.TRAIN_SAMPLES_NEEDED
                       else "All samples captured — ready to train.")
        else:
            message = quality.message
        phase = "ready" if count >= self.TRAIN_SAMPLES_NEEDED else "capture"
        self._trainer.message = message
        self.bus.publish("train_sample", ok=quality.ok, message=message,
                         count=count, need=self.TRAIN_SAMPLES_NEEDED,
                         phase=phase, seconds=round(quality.seconds, 2))
        self.bus.publish("train_update", phase=phase, phrase=self._trainer.phrase,
                         count=count, need=self.TRAIN_SAMPLES_NEEDED,
                         message=message)

    def training_status(self) -> dict:
        if self._trainer is None:
            return {"active": False}
        return {"active": True, "phrase": self._trainer.phrase,
                "count": len(self._trainer.samples),
                "need": self.TRAIN_SAMPLES_NEEDED,
                "listening": self._train_capture_armed,
                "message": self._trainer.message}

    def training_cancel(self) -> dict:
        self._cancel_training_capture_timeout()
        self._trainer = None
        self._train_capture_armed = False
        self._train_vad.reset()
        self.bus.publish("train_update", phase="idle", count=0,
                         need=self.TRAIN_SAMPLES_NEEDED)
        return {"ok": True}

    async def training_finish(self) -> dict:
        if self._train_capture_armed:
            return {"ok": False,
                    "message": "Finish this recording first — say the phrase or start over."}
        if self._trainer is None or len(self._trainer.samples) < self.TRAIN_SAMPLES_MINIMUM:
            return {"ok": False,
                    "message": "Record a few more samples first — three at minimum."}
        from pathlib import Path as _Path

        from .wakeword_trainer import save_template, slugify, train_wake

        trainer = self._trainer
        phrase = trainer.phrase

        def work():
            # Re-embed once here so train_wake receives feature-ready float32
            # arrays straight from the user's recordings.
            return train_wake(trainer.samples)

        try:
            trained = await self.loop.run_in_executor(None, work)
        except Exception as exc:
            self.bus.publish("log", line=f"training failed: {exc}")
            return {"ok": False, "message": f"Training didn't take: {exc}"}

        out_dir = _Path(self.cfg.data_dir) / "wakewords"
        path = save_template(trained, phrase, out_dir / f"{slugify(phrase)}.npz")

        # Go live immediately and remember the choice.
        self.cfg.wake.models = [str(path)]
        self.cfg.wake.mode = "openwakeword"
        self.cfg.wake.phrase = phrase
        try:
            config_mod.write_overrides(self.cfg.data_dir, {"wake": {
                "models": [str(path)], "mode": "openwakeword", "phrase": phrase}})
        except OSError as exc:
            self.bus.publish("log", line=f"could not persist wake model: {exc}")
        self._remember_mtimes()
        self._wake = self._build_wake()
        self._cancel_training_capture_timeout()
        self._trainer = None
        self._train_capture_armed = False
        self.bus.publish("train_update", phase="done", phrase=phrase,
                         threshold=round(trained.threshold, 3),
                         margin=round(trained.margin, 3))
        self.bus.publish("log", line=f"Wake phrase “{phrase}” trained and active.")
        return {"ok": True, "phrase": phrase, "path": str(path),
                "threshold": round(trained.threshold, 3),
                "margin": round(trained.margin, 3),
                "engine": type(self._wake).__name__}

    # ------------------------------------------------------------------ #
    # In-app component install (Setup panel — nothing typed, ever)         #
    # ------------------------------------------------------------------ #

    def start_setup(self) -> dict:
        """Begin the component install *without* holding an HTTP request open.

        The installer takes minutes; the client that asked for it should get an
        answer in milliseconds and then watch `setup_progress` events (SSE).
        Returns the honest reason when it can't start.
        """
        if config_mod.resolved_profile(self.cfg) == "demo":
            return {"ok": False, "started": False,
                    "message": "Development build — component install runs on a real install."}
        if self._setup_running:
            return {"ok": False, "started": False,
                    "message": "An install is already running."}
        # Claim the slot here (not inside the coroutine) so two quick taps on
        # "Install" can't both start one.
        self._setup_running = True

        async def _install() -> None:
            try:
                await self.loop.run_in_executor(None, self._run_setup_sync)
            except Exception as exc:
                detail = log_exception("install failed", exc, logger=log)
                self.bus.publish("log", line=f"install failed: {detail}")
                # The UI's buttons live and die by `setup_done`: without it
                # the Setup panel stays in "Installing…" forever, and every
                # button there is disabled. Always settle the account.
                self.bus.publish("setup_done", ok=False,
                                 summary=f"Install stopped: {detail[:120]}")
            finally:
                self._setup_running = False
        self._laya_idle_unloaded = False

        asyncio.run_coroutine_threadsafe(_install(), self.loop)
        return {"ok": True, "started": True,
                "message": "Install started — progress appears in Setup."}

    async def run_setup(self) -> dict:
        if self._setup_running:
            return {"ok": False, "message": "An install is already running."}
        if config_mod.resolved_profile(self.cfg) == "demo":
            return {"ok": False,
                    "message": "Development build — component install runs on a real install."}
        self._setup_running = True
        try:
            await self.loop.run_in_executor(None, self._run_setup_sync)
        except Exception as exc:
            detail = log_exception("install failed", exc, logger=log)
            # Same contract as start_setup: the UI must always see setup_done.
            self.bus.publish("setup_done", ok=False,
                             summary=f"Install stopped: {detail[:120]}")
            return {"ok": False, "message": f"Install stopped: {detail[:120]}"}
        finally:
            self._setup_running = False
        self._laya_idle_unloaded = False
        return {"ok": True}

    async def run_setup_step(self, key: str) -> Any:
        """One Setup step at a time — the per-card Install buttons."""
        from .setup_installer import StepResult

        if config_mod.resolved_profile(self.cfg) == "demo":
            return StepResult(key, "Development build", "skip",
                              "Component install runs on a real Mac install.")
        if self._setup_running:
            return StepResult(key, "Install already running", "fail",
                              "One install at a time — this one is in progress.")
        self._setup_running = True
        try:
            return await self.loop.run_in_executor(None, self._run_setup_step_sync, key)
        finally:
            self._setup_running = False
        self._laya_idle_unloaded = False

    def _engine_repo(self):
        """Where the `python` step pip-installs from: the engine folder the
        app knows about (AURA_ENGINE_PATH), else this package's repo."""
        import os
        from pathlib import Path as _Path

        env = os.environ.get("AURA_ENGINE_PATH", "")
        if env and (_Path(env) / "pyproject.toml").is_file():
            return _Path(env)
        return _Path(__file__).resolve().parent.parent

    def _run_setup_step_sync(self, key: str) -> Any:
        from .setup_installer import run_step

        return run_step(self, self._engine_repo(), key)

    def _run_setup_sync(self) -> None:
        from .setup_installer import run_installer

        run_installer(self, self._engine_repo())

    # ------------------------------------------------------------------ #
    # Confirmation / feedback API (called from server threads)             #
    # ------------------------------------------------------------------ #

    async def set_config(self, updates: dict) -> dict:
        """Live settings from the UI: validate, persist, hot-apply. Runs on
        the loop because some fields (wake engine, STT) need rebuilding."""
        from .config import LIVE_FIELDS, load_config, write_overrides

        if not isinstance(updates, dict) or not updates:
            return {"ok": False, "applied": [], "message": "nothing to change"}
        for section, values in updates.items():
            if section not in LIVE_FIELDS:
                return {"ok": False, "applied": [],
                        "message": f"“{section}” isn't a setting Aura can change live"}
            if not isinstance(values, dict):
                return {"ok": False, "applied": [], "message": f"“{section}” needs a field map"}
            for key, value in values.items():
                if key not in LIVE_FIELDS[section]:
                    return {"ok": False, "applied": [],
                            "message": f"{section}.{key} isn't changeable live"}
                current = getattr(getattr(self.cfg, section), key)
                if isinstance(current, bool):
                    if not isinstance(value, bool):
                        return _config_type_error(section, key, "on/off")
                elif isinstance(current, (int, float)):
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        return _config_type_error(section, key, "a number")
                elif isinstance(current, str):
                    if not isinstance(value, str):
                        return _config_type_error(section, key, "text")
                elif isinstance(current, list):
                    if not isinstance(value, list):
                        return _config_type_error(section, key, "a list")
                else:
                    return {"ok": False, "applied": [],
                            "message": f"{section}.{key} isn't supported"}

        try:
            write_overrides(self.cfg.data_dir, updates)
        except OSError as exc:
            return {"ok": False, "applied": [],
                    "message": f"changed for now, but couldn't save it: {exc}"}

        try:
            fresh = load_config(data_dir=self.cfg.data_dir)
        except Exception as exc:
            fresh = None
            self.bus.publish("log", line=f"config reload after save failed: {exc}")
        if fresh is not None:
            self.apply_live_config(fresh)
        else:
            for section, values in updates.items():
                for key, value in values.items():
                    setattr(getattr(self.cfg, section), key, value)
        self._remember_mtimes()  # the file we just wrote is expected
        applied = [f"{s}.{k}" for s, vs in updates.items() for k in vs]
        self.bus.publish("config", changed=applied)
        self.bus.publish("log", line="settings updated: " + ", ".join(applied))
        return {"ok": True, "applied": applied, "message": "Saved."}

    def run_blocking(self, fn, *args, timeout: float = 90.0) -> Any:
        """Run a blocking function (TCC prompt, probe, installer step) on the
        orchestrator's loop without stalling it, called from a server thread."""
        async def _run():
            return await self.loop.run_in_executor(None, fn, *args)

        try:
            return asyncio.run_coroutine_threadsafe(_run(), self.loop).result(
                timeout=timeout)
        except Exception as exc:
            return ("error", f"that didn't finish in time ({exc.__class__.__name__})")

    def resolve_confirmation(self, token: str, answer: Literal["confirm", "cancel"]) -> bool:
        """Resolve a pending proposal. May be called from the server thread —
        the wake-up must be scheduled onto the loop that awaits the future,
        or the session would hang in `proposing` forever."""
        fut = self._confirmations.get(token)
        if fut is None or fut.done():
            log.info("proposal %s: %r arrived too late — the proposal is gone",
                     token, answer)
            return False
        log.info("proposal %s: answered %r", token, answer)

        def _set() -> None:
            if not fut.done():
                fut.set_result(answer)

        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if self._loop is not None and here is not self._loop:
            self._loop.call_soon_threadsafe(_set)
        else:
            _set()
        return True

    def record_feedback(self, transcript: str, skill: str, verdict: str, note: str = "",
                        args: dict | None = None) -> None:
        """Explicit 👍/👎 from the Activity timeline — the richest signal we get.

        The verdict becomes a supervised example for the Laya fine-tune
        (`ExampleBuffer`), keyed by the skill the verdict is about. A feedback
        row without a skill cannot supervise anything, so it is stored as a
        preference note at most, never as a bogus ""-skill example.
        """
        if skill:
            self._record_example(transcript, _ShadowAction(skill, args or {}), verdict)
            log.info("feedback: %s %r on %r", verdict, skill, transcript[:60])
        else:
            log.info("feedback: %r on %r arrived without a skill — kept as a note only",
                     verdict, transcript[:60])
        if note:
            key, value = self.memory.extract_preference(note)
            self.memory.set_preference(key, value, source="user")
        self.bus.publish("feedback", skill=skill, verdict=verdict)

    def metrics(self) -> dict[str, Any]:
        """Lightweight self-observation — the honest kind of dashboard."""
        import resource
        import sys as _sys

        # ru_maxrss: bytes on macOS, KB on Linux → normalize to MB.
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss_mb / (1024 * 1024) if _sys.platform == "darwin" else rss_mb / 1024
        wake = self.wake_status()
        return {
            "rss_mb": round(rss_mb, 1),
            "state": self.state,
            "wake_mode": self.cfg.wake.mode,
            "wake_engine": wake["engine"],
            "wake_active": wake["active"],
            "wake_error": wake["error"],
            "wake_detail": wake["detail"],
            "stt_engine": type(self.stt).__name__,
            "examples": self.examples.stats(),
        }

    # ------------------------------------------------------------------ #

    def _record_example(self, transcript: str, action: Action | _ShadowAction, outcome: str,
                        decision=None) -> None:
        """Record one supervised example for the Laya fine-tune.

        The decision is passed in whenever the session already computed it —
        re-asking the model here used to be a second forward pass per action,
        on the confirmation path, for a number we already had.
        """
        if isinstance(action, Action):
            skill, args = action.skill, action.args
        else:
            skill, args = action.skill, {}
        if decision is None:
            try:
                decision = self.laya.decide(transcript, skill, args, "")
            except Exception as exc:
                detail = log_exception(f"examples: the gate raised while labelling {skill} — "
                                       "using the offline scorer", exc, logger=log)
                decision = HeuristicBackend().decide(transcript, skill, args, "")
                decision.error = detail
                decision.source = "fallback"
        if outcome in ("auto", "confirmed"):
            match, destructive = 1.0, decision.destructive
        elif outcome == "corrected":
            match, destructive = 0.0, decision.destructive
        else:  # cancelled
            match, destructive = 0.0, 1.0
        weight = 2.0 if outcome in ("corrected", "cancelled") else 1.0
        self.examples.record(transcript, skill, args, outcome, match, destructive, weight)


# --------------------------------------------------------------------------- #
# Small internal shims (kept boring on purpose)                                #
# --------------------------------------------------------------------------- #


class _RawFrame:
    """Minimal frame wrapper for executor-bound STT calls."""

    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm


def _execute_skill(skill, args: dict, ctx: _SkillCtx):
    """Run one skill on a worker thread, off the engine's event loop.

    Skills are coroutines whose bodies are blocking macOS calls; a throwaway
    event loop per call lets the main loop keep serving SSE, health checks and
    the next command while a skill waits on the system.
    """
    return asyncio.run(skill.execute(args, ctx))


class _SkillCtx:
    __slots__ = ("bridge", "config", "memory")

    def __init__(self, orch: Orchestrator) -> None:
        self.bridge = orch.bridge
        self.memory = orch.memory
        self.config = orch.cfg


class _SkillOutcome:
    __slots__ = ("data", "message", "ok")

    def __init__(self, ok: bool, message: str, data: dict | None = None) -> None:
        self.ok, self.message, self.data = ok, message, data or {}


def _skill_result(ok: bool, message: str, data: dict | None = None):
    from .skills.base import SkillResult
    return SkillResult(ok, message, data or {})


class _ShadowAction:
    """Feedback arrives per skill name (+ the action's args when the timeline
    still has them); that's enough supervision for the buffer."""

    def __init__(self, skill: str, args: dict | None = None) -> None:
        self.skill = skill
        self.args = args or {}
        self.why = ""


def _config_type_error(section: str, key: str, want: str) -> dict:
    return {"ok": False, "applied": [],
            "message": f"{section}.{key} needs {want}, Aura got something else"}


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
