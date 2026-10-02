"""Aura CLI — a keyboardless, voice-first navigator for your Mac.

    python -m aura serve        start the engine (the app talks to it over loopback)
    python -m aura talk         talk to Aura right now (mic if present, text otherwise)
    python -m aura hear F.wav   run one spoken utterance (a WAV file) through the
                                whole pipeline and print what Aura does
    python -m aura doctor       capability matrix (what will run on this machine)
    python -m aura laya-check   prove the decision model works, with timings

Aura's brain is Laya — a non-autoregressive decision model that answers typed
questions in one forward pass. There is no LLM anywhere: nothing to serve,
nothing to download gigabytes for, no "still thinking". `talk` and `hear`
exist so the product can be *proven* end-to-end on any machine — voice in,
navigation out, spoken reply — without a menu-bar app in the loop.

Every command configures the same logging tree (`aura/log.py`), so running the
engine by hand produces the same lines the app's Activity feed shows — and when
something fails, `laya-check` says exactly what and where.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time


def _profile_for(cfg) -> str:
    from .config import resolved_profile

    return resolved_profile(cfg)


# --------------------------------------------------------------------------- #
# Shared headless stack — one wiring, used by serve/talk/hear                  #
# --------------------------------------------------------------------------- #


def _build_stack(cfg):
    """The exact wiring `serve` uses, minus the HTTP server.

    Returns (orchestrator, extras-dict). Everything is real: the Laya gate,
    the skill registry, memory — only the I/O surfaces (mic, STT, TTS) depend
    on what this machine actually has.
    """
    from pathlib import Path

    from . import log as log_mod
    from .events import EventBus
    from .laya import ExampleBuffer, build_backend
    from .memory import Memory
    from .orchestrator import Orchestrator
    from .planner import build_planner
    from .safety import SafetyGate
    from .skills import build_default_registry
    from .stt import build_stt
    from .tts import build_tts

    data_dir = Path(cfg.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    bus = EventBus()
    log_mod.setup(data_dir, level=cfg.logging.level, event_level=cfg.logging.event_level,
                  file=cfg.logging.file)
    log_mod.attach_bus(bus, cfg.logging.event_level)

    bridge = _build_bridge(cfg)
    registry = build_default_registry(bridge)
    # The decision backend comes first: the planner routes *through* it.
    laya_backend = build_backend(cfg, bus)
    planner = build_planner(cfg, registry.catalog_prompt(), registry.specs(), laya_backend)
    safety = SafetyGate(cfg, laya_backend)
    memory = Memory(data_dir / "aura.sqlite3")
    examples = ExampleBuffer(data_dir / "aura.sqlite3")
    stt = build_stt(cfg)
    tts = build_tts(cfg)

    orch = Orchestrator(cfg=cfg, bus=bus, registry=registry, bridge=bridge,
                        planner=planner, laya_backend=laya_backend, safety=safety,
                        memory=memory, examples=examples, stt=stt, tts=tts)
    return orch, {"bus": bus, "laya": laya_backend, "planner": planner}


def _build_bridge(cfg):
    from .skills import DryRunBridge, MacBridge

    if cfg.profile == "demo":
        return DryRunBridge()
    if sys.platform == "darwin":
        return MacBridge()
    return DryRunBridge()


async def _wait_for_reply(bus, sid: int, timeout: float = 60.0) -> str:
    """Drain an existing bus subscription until the session answers.

    The caller must subscribe *before* submitting the request — a session
    answers synchronously, so a subscription created afterwards misses the
    reply event entirely (the original "never got a response" bug). Aura's
    contract is that she always answers; the timeout only guards against a
    bug hanging the CLI.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "(no reply — the engine broke its always-answer contract)"
        try:
            ev = await asyncio.wait_for(bus.get(sid), timeout=remaining)
        except TimeoutError:
            return "(no reply — the engine broke its always-answer contract)"
        if ev.type == "reply":
            return str(ev.data.get("text") or "")
        if ev.type == "transcript":
            print(f"  heard: “{ev.data.get('text', '')}”")
        elif ev.type == "proposal":
            print(f"  → Aura asks permission (token {ev.data.get('token')}): "
                  + ", ".join(a.get("skill", "?") for a in ev.data.get("actions", [])))


# --------------------------------------------------------------------------- #
# talk — the keyboardless loop, right here in the terminal                     #
# --------------------------------------------------------------------------- #


def cmd_talk(cfg) -> int:
    """Talk to Aura. With a microphone this is the real voice loop (wake →
    listen → transcribe → Laya → act → speak). Without one, typed lines go
    through the identical session path — every stage after the ear is the same.
    """
    from . import log as log_mod

    orch, extras = _build_stack(cfg)
    bus = extras["bus"]
    log = log_mod.get_logger("talk")

    async def main() -> None:
        await orch.start()
        has_mic = orch._has_audio
        laya_status = orch.laya.status() if hasattr(orch.laya, "status") else {}
        print(f"Aura — profile={_profile_for(cfg)} bridge={orch.bridge.platform} "
              f"planner={type(orch.planner).__name__} "
              f"laya={'ready' if laya_status.get('ready') else 'loading/offline'}")
        if has_mic:
            mode = getattr(cfg.wake, "mode", "manual")
            print("Microphone live — say your command." if mode != "manual"
                  else "Microphone live — trigger with POST /api/trigger or ⌥Space, "
                       "or type below.")
        else:
            print("No microphone here — type your commands; Ctrl-D or 'exit' to quit.")
        print("Try: “open safari” · “set volume to 30” · “quiet the house” · "
              "“search for airport lounges”\n")
        try:
            while True:
                try:
                    line = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: input("aura> "))
                except EOFError:
                    break
                line = line.strip()
                if not line:
                    continue
                if line.lower() in ("exit", "quit"):
                    break
                # Subscribe FIRST: the session answers synchronously, so the
                # reply event is gone by the time submit_text returns.
                sid = bus.subscribe_async()
                try:
                    await orch.submit_text(line)
                    reply = await _wait_for_reply(bus, sid)
                finally:
                    bus.unsubscribe_async(sid)
                print(f"Aura: {reply}\n")
        finally:
            await orch.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")
    log.debug("talk: session ended")
    return 0


# --------------------------------------------------------------------------- #
# hear — one WAV file through the whole voice pipeline                         #
# --------------------------------------------------------------------------- #


def cmd_hear(cfg, wav_path: str) -> int:
    """Voice in, navigation out, without a microphone.

    Reads a WAV file (any sample rate — it is resampled to 16 kHz mono),
    pushes it through the same VAD → STT → Laya → skills path the microphone
    uses, and prints the transcript, the plan, and the spoken reply. This is
    the end-to-end proof that the voice pipeline works, on any machine.
    """
    import wave as wave_mod

    from . import log as log_mod

    orch, extras = _build_stack(cfg)
    bus = extras["bus"]
    log = log_mod.get_logger("hear")

    try:
        with wave_mod.open(wav_path, "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except Exception as exc:
        print(f"Cannot read {wav_path}: {exc}")
        return 2
    if width != 2:
        print(f"{wav_path} is {width * 8}-bit — Aura expects 16-bit PCM.")
        return 2

    try:
        import numpy as np

        pcm = np.frombuffer(raw, dtype=np.int16)
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
        if rate != 16_000:
            n_out = int(len(pcm) * 16_000 / rate)
            pcm = np.interp(np.linspace(0, len(pcm) - 1, n_out),
                            np.arange(len(pcm)), pcm).astype(np.int16)
    except ImportError:
        if channels != 1 or rate != 16_000:
            print("Resampling needs numpy — pip install numpy (or pass a "
                  "16 kHz mono WAV).")
            return 2
        pcm = None

    from .audio import AudioFrame

    # Build one frame per ~32 ms chunk, the way the microphone stream does.
    frames = []
    if pcm is not None:
        import numpy as np

        chunk_samples = 512
        for i in range(0, len(pcm), chunk_samples):
            part = pcm[i:i + chunk_samples]
            if part.size == 0:
                break
            frames.append(AudioFrame(pcm=part.astype("<i2"), ts=i / 16_000.0))
    else:
        chunk = 16_000 * 2 * 32 // 1000       # bytes per 32 ms at 16 kHz mono
        for i in range(0, len(raw), chunk):
            part = raw[i:i + chunk]
            if len(part) < 2:
                break
            frames.append(AudioFrame(pcm=_i16(part), ts=i / (16_000 * 2)))

    async def main() -> None:
        await orch.start()
        sid = bus.subscribe_async()  # before the session: it answers synchronously
        try:
            print(f"hearing {wav_path} ({len(frames) * 0.03:.1f}s of audio)…")
            await orch.run_session_frames(frames)
            reply = await _wait_for_reply(bus, sid)
            print(f"Aura: {reply}")
        finally:
            bus.unsubscribe_async(sid)
            await orch.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    log.debug("hear: done")
    return 0


def _i16(data: bytes):
    import array

    out = array.array("h")
    out.frombytes(data[: len(data) // 2 * 2])
    return out


# --------------------------------------------------------------------------- #
# doctor                                                                       #
# --------------------------------------------------------------------------- #


def cmd_doctor(cfg) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        checks.append((name, ok, note))

    check("profile", True, _profile_for(cfg))
    check("data dir", True, cfg.data_dir)

    for mod in ("numpy", "sounddevice", "openwakeword", "faster_whisper", "laya"):
        try:
            __import__(mod)
            check(f"python: {mod}", True)
        except Exception as exc:
            check(f"python: {mod}", False, str(exc)[:60])

    import shutil

    for binary in ("say", "osascript", "pbcopy", "whisper-cli"):
        check(f"binary: {binary}", shutil.which(binary) is not None,
              shutil.which(binary) or "not on PATH")

    check("platform is macOS", sys.platform == "darwin",
          "demo profile elsewhere" if sys.platform != "darwin" else "")

    # The decision model is the brain: report what it can actually do, not
    # merely whether a package name imports.
    try:
        from . import log as log_mod
        from .laya import build_backend, laya_available, resolve_checkpoint_dir

        checkpoint, source = resolve_checkpoint_dir(cfg)
        check("laya package", laya_available(),
              "install with `pip install laya`" if not laya_available() else "importable")
        check("laya checkpoint", bool(checkpoint),
              f"{checkpoint} ({source})" if checkpoint
              else f"{source} — train one: python scripts/train_navigation_laya.py")
        backend = build_backend(cfg)
        status = backend.status() if hasattr(backend, "status") else {}
        check("laya backend", bool(status.get("ready")),
              f"{status.get('backend', '?')}"
              + (f" — {status.get('error')}" if status.get("error") else "")
              + ("  (run `python -m aura laya-check`)" if not status.get("ready") else ""))
        check("planner", cfg.planner.engine,
              f"planner.engine={cfg.planner.engine} (the brain is Laya)")
        check("engine log", True, str(log_mod.log_status().get("file") or "stderr only"))
    except Exception as exc:
        check("laya backend", False, f"{exc.__class__.__name__}: {exc}")

    if _profile_for(cfg) == "mac":
        # Live capability checks — the same honesty the Setup panel shows.
        from . import permissions as perms

        check("microphone", perms.request_microphone()[0] == "ok",
              "open it in Aura's Setup panel" if perms.is_mac() else "")
        check("accessibility", perms.check_accessibility() is True,
              "grant in System Settings (Aura's Setup panel opens it)")
        check("wake models", perms.check_wake_models(cfg)[0],
              perms.check_wake_models(cfg)[1])
        check("whisper model", perms.check_whisper_cpp(cfg),
              cfg.stt.whisper_model or "no model path configured")
        check("engine token", True, str(__import__("aura.localauth", fromlist=["localauth"])
              .token_path(cfg.data_dir)))
        check("Aura.app installed",
              shutil.which("open") is not None and
              (__import__("pathlib").Path("/Applications/Aura.app").is_dir()),
              "/Applications/Aura.app" if __import__("pathlib").Path("/Applications/Aura.app").is_dir()
              else "run ./scripts/install.sh")

    print("Aura doctor — capability matrix")
    print("=" * 52)
    for name, ok, note in checks:
        mark = "✔" if ok else "✘"
        print(f"  {mark}  {name:20s} {note}")
    print("=" * 52)
    print("Run `python -m aura talk` to speak with Aura right now.")
    print("Brain not working? `python -m aura laya-check` proves it end-to-end.")
    return 0


# --------------------------------------------------------------------------- #
# laya-check — the one command that answers "is the brain actually working?"   #
# --------------------------------------------------------------------------- #

#: Real decisions, with the answers a working model gives. Not a benchmark —
#: a *smoke test* whose purpose is to fail loudly, with the reason, instead of
#: letting Aura quietly run the offline gate for weeks.
LAYA_SMOKE_CASES: list[tuple[str, str, dict, bool, bool]] = [
    # transcript, skill, args, should match, should look destructive
    ("open spotify", "system.open_app", {"app": "Spotify"}, True, False),
    ("set volume to 30", "system.set_volume", {"level": 30}, True, False),
    ("empty the trash", "system.empty_trash", {}, True, True),
    ("what is the weather", "system.open_app", {"app": "Terminal"}, False, False),
]

LAYA_SMOKE_ROUTES: list[tuple[str, str]] = [
    ("open spotify", "system.open_app"),
    ("quiet the house", "system.toggle_dnd"),
]


def cmd_laya_check(cfg) -> int:
    """Prove the Laya install end-to-end: import → load → decide → route."""
    from . import log as log_mod
    from .laya import (
        LayaGate,
        RealLayaBackend,
        build_backend,
        laya_available,
        resolve_checkpoint_dir,
    )

    log_mod.setup(cfg.data_dir, level=cfg.logging.level or "info",
                  event_level="error", file=cfg.logging.file)
    log = log_mod.get_logger("laya-check")
    print("Aura — Laya self-test")
    print("=" * 62)

    print(f"  package           : {'importable' if laya_available() else 'NOT FOUND'}")
    if not laya_available():
        print("  reason            : `import laya` fails — install it with `pip install laya`")
        print("  fallback          : the deterministic offline gate answers every question")
        print("  log               : " + str(log_mod.log_path() or "stderr"))
        return 2

    checkpoint, source = resolve_checkpoint_dir(cfg)
    print(f"  checkpoint        : {checkpoint or 'hub default'}  [{source}]")

    backend = build_backend(cfg)
    if isinstance(backend, LayaGate):
        inner = backend.real
    elif isinstance(backend, RealLayaBackend):
        inner = backend
    else:
        print("  backend           : offline gate deliberately configured "
              f"(laya.backend={cfg.laya.backend})")
        return 0

    print(f"  device            : {inner.device or 'auto'}")
    print("  loading           : this may download weights on the first run…", flush=True)
    started = time.perf_counter()
    try:
        inner.load()
    except Exception as exc:
        print(f"  LOAD FAILED       : {exc}")
        print(f"  traceback         : see the log ({log_mod.log_path() or 'stderr'})")
        return 2
    loaded_ms = (time.perf_counter() - started) * 1000.0
    status = inner.status()
    print(f"  version           : {status.get('version') or 'unknown'}")
    print(f"  checkpoints       : {', '.join(status.get('loaded') or []) or '(none reported)'}"
          f"  [{loaded_ms:.0f} ms to load]")
    print("-" * 62)
    print("  gate decisions (match / destructive, both in ONE forward pass):")
    failures = 0
    for transcript, skill, args, want_match, want_destructive in LAYA_SMOKE_CASES:
        try:
            decision = inner.decide(transcript, skill, args, "smoke test")
        except Exception as exc:
            failures += 1
            print(f"    ✘ {transcript!r} → {type(exc).__name__}: {exc}")
            log.error("smoke decision failed for %r — %s", transcript,
                      log_mod.describe_exception(exc))
            continue
        marks = []
        if (decision.match >= 0.5) != want_match:
            marks.append(f"expected match {'≥' if want_match else '<'} 0.5")
        if (decision.destructive >= 0.5) != want_destructive:
            marks.append(f"expected destructive {'≥' if want_destructive else '<'} 0.5")
        if marks:
            failures += 1
        print(f"    {'✘' if marks else '·'} {transcript!r:24s} match={decision.match:.2f} "
              f"destructive={decision.destructive:.2f} model={decision.model or '?'} "
              f"({decision.ms:.0f} ms)"
              + (f"   ← {', '.join(marks)}" if marks else ""))
    print("-" * 62)
    print("  routing (choice questions over the skill catalog):")
    specs = None
    try:
        from .skills import build_default_registry

        specs = build_default_registry().specs()
    except Exception as exc:                        # pragma: no cover - defensive
        log.warning("could not build the skill registry for routing checks: %s", exc)
    if specs:
        from .intent import LayaRouter

        router = LayaRouter(inner, specs)
        for transcript, expected in LAYA_SMOKE_ROUTES:
            try:
                result = router.route(transcript)
            except Exception as exc:               # a diagnostic tool must not crash
                failures += 1
                print(f"    ✘ {transcript!r} → {type(exc).__name__}: {exc}")
                log.error("smoke route failed for %r — %s", transcript,
                          log_mod.describe_exception(exc))
                continue
            if result.skill != expected:
                failures += 1
            mark = "·" if result.skill == expected else "✘"
            print(f"    {mark} {transcript!r:24s} → {result.skill or '(none)'} "
                  f"p={result.confidence:.2f} ({result.ms:.0f} ms)"
                  + ("" if result.skill == expected else f"   [expected {expected}]")
                  + (f"   {result.reason}" if result.error else ""))
    print("-" * 62)
    if failures:
        print(f"  RESULT: FAILED — {failures} decision(s) raised; see the log for tracebacks")
        return 2
    print("  RESULT: Laya is working. If Aura still answers from the offline gate, check "
          "`laya.backend` in your config and the engine log.")
    return 0


# --------------------------------------------------------------------------- #
# serve                                                                        #
# --------------------------------------------------------------------------- #


def cmd_serve(cfg) -> int:
    from . import log as log_mod
    from .server import TOKEN_HEADER, AuraServer

    orch, extras = _build_stack(cfg)
    log = log_mod.get_logger()
    log.info("engine starting — profile=%s bridge=%s planner=%s laya=%s",
             _profile_for(cfg), orch.bridge.platform, type(orch.planner).__name__,
             type(orch.laya).__name__)
    server = AuraServer(orch, cfg)

    async def main() -> None:
        await orch.start()
        try:
            server.start()
        except OSError as exc:
            # Port already bound — say what's wrong, don't dump a traceback.
            print(f"\nPort {cfg.server.port} is already in use — is Aura already running?")
            print("(Change the port: AURA_PORT=<port> python -m aura serve)\n")
            raise SystemExit(1) from exc
        except RuntimeError as exc:
            print(f"\n{exc}\n")
            raise SystemExit(1) from exc
        from . import localauth

        host = "127.0.0.1" if cfg.server.host == "0.0.0.0" else cfg.server.host
        print(f"Aura v{__import__('aura', fromlist=['__version__']).__version__} "
              f"— profile={_profile_for(cfg)} bridge={orch.bridge.platform}")
        print(f"Engine:  http://{host}:{cfg.server.port}  (JSON API — the app is the UI)")
        print(f"Data:    {cfg.data_dir}")
        if server.token:
            print(f"Token:   {localauth.token_path(cfg.data_dir)}  (send it as {TOKEN_HEADER} = X-Aura-Token)")
        else:
            print("Token:   DISABLED — anyone on this Mac can drive Aura (AURA_NO_AUTH=1).")
        if not localauth.is_loopback_host(cfg.server.host):
            print("WARNING: the engine is bound beyond loopback — anyone who can reach "
                  "this port can drive Aura.")
        print("Press Ctrl-C to quit.")
        try:
            await asyncio.Event().wait()
        finally:
            server.stop()
            await orch.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import __version__
    from .config import load_config

    parser = argparse.ArgumentParser(prog="aura",
                                     description="Aura — keyboardless voice navigation for your Mac.")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "talk", "hear", "doctor", "laya-check"])
    parser.add_argument("target", nargs="?", default="",
                        help="the WAV file for `hear`")
    parser.add_argument("--config", help="path to a config.toml")
    parser.add_argument("--version", action="version", version=f"aura {__version__}")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.command == "doctor":
        return cmd_doctor(cfg)
    if args.command == "laya-check":
        return cmd_laya_check(cfg)
    if args.command == "talk":
        return cmd_talk(cfg)
    if args.command == "hear":
        if not args.target:
            print("usage: python -m aura hear <utterance.wav>")
            return 2
        return cmd_hear(cfg, args.target)
    return cmd_serve(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
