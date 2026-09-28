"""Aura CLI.

    python -m aura serve     start the engine (the app talks to it over loopback)
    python -m aura doctor    capability matrix (what will run on this machine)

There is no browser UI: the product is the native macOS app. `serve` exists so
the engine can run head-less on any machine — for the test suite, for CI, and
for developers poking at the API with a token.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _profile_for(cfg) -> str:
    from .config import resolved_profile

    return resolved_profile(cfg)


def cmd_doctor(cfg) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        checks.append((name, ok, note))

    check("profile", True, _profile_for(cfg))
    check("data dir", True, cfg.data_dir)

    for mod in ("numpy", "sounddevice", "openwakeword", "faster_whisper", "httpx", "laya"):
        try:
            __import__(mod)
            check(f"python: {mod}", True)
        except Exception as exc:
            check(f"python: {mod}", False, str(exc)[:60])

    import shutil
    import urllib.request

    for binary in ("say", "osascript", "pbcopy", "whisper-cli", "ollama"):
        check(f"binary: {binary}", shutil.which(binary) is not None,
              shutil.which(binary) or "not on PATH")

    check("platform is macOS", sys.platform == "darwin",
          "demo profile elsewhere" if sys.platform != "darwin" else "")

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
        if cfg.planner.engine != "mock":
            try:
                url = cfg.planner.base_url.rstrip("/") + "/models"
                with urllib.request.urlopen(url, timeout=2) as resp:
                    live = resp.status == 200
            except Exception:
                live = False
            check("planner server", live,
                  f"{cfg.planner.base_url} ({cfg.planner.model})" +
                  (" — is Ollama running?" if not live else ""))
        else:
            check("planner server", True, "mock (basic mode)")
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
    print("Run `python -m aura serve` to start the engine (or open Aura from /Applications).")
    return 0


def cmd_serve(cfg) -> int:
    from pathlib import Path

    from .events import EventBus
    from .laya import ExampleBuffer, build_backend
    from .memory import Memory
    from .orchestrator import Orchestrator
    from .planner import build_planner
    from .safety import SafetyGate
    from .server import TOKEN_HEADER, AuraServer
    from .skills import build_default_registry
    from .stt import build_stt
    from .tts import build_tts

    data_dir = Path(cfg.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    bus = EventBus()
    bridge = _build_bridge(cfg)
    registry = build_default_registry(bridge)
    planner = build_planner(cfg, registry.catalog_prompt())
    laya_backend = build_backend(cfg)
    safety = SafetyGate(cfg, laya_backend)
    memory = Memory(data_dir / "aura.sqlite3")
    examples = ExampleBuffer(data_dir / "aura.sqlite3")
    stt = build_stt(cfg)
    tts = build_tts(cfg)

    orch = Orchestrator(cfg=cfg, bus=bus, registry=registry, bridge=bridge,
                        planner=planner, laya_backend=laya_backend, safety=safety,
                        memory=memory, examples=examples, stt=stt, tts=tts)
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
              f"— profile={_profile_for(cfg)} bridge={bridge.platform}")
        print(f"Engine:  http://{host}:{cfg.server.port}  (JSON API — the app is the UI)")
        print(f"Data:    {data_dir}")
        if server.token:
            print(f"Token:   {localauth.token_path(data_dir)}  (send it as {TOKEN_HEADER} = X-Aura-Token)")
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

def _build_bridge(cfg):
    from .skills import DryRunBridge, MacBridge

    if cfg.profile == "demo":
        return DryRunBridge()
    if sys.platform == "darwin":
        return MacBridge()
    return DryRunBridge()


def main(argv: list[str] | None = None) -> int:
    from . import __version__
    from .config import load_config

    parser = argparse.ArgumentParser(prog="aura", description="Aura — your Mac, at your command.")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "doctor"])
    parser.add_argument("--config", help="path to a config.toml")
    parser.add_argument("--version", action="version", version=f"aura {__version__}")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.command == "doctor":
        return cmd_doctor(cfg)
    return cmd_serve(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
