"""Aura CLI.

    python -m aura serve     start the orchestrator + local UI
    python -m aura doctor    capability matrix (what will run on this machine)
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _profile_for(cfg) -> str:  # noqa: ANN001
    if cfg.profile != "auto":
        return cfg.profile
    return "mac" if sys.platform == "darwin" else "demo"


def cmd_doctor(cfg) -> int:  # noqa: ANN001
    from .config import load_config  # noqa: F401

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

    for binary in ("say", "osascript", "pbcopy", "whisper-cli", "ollama"):
        check(f"binary: {binary}", shutil.which(binary) is not None,
              shutil.which(binary) or "not on PATH")

    check("platform is macOS", sys.platform == "darwin",
          "demo profile elsewhere" if sys.platform != "darwin" else "")

    print("Aura doctor — capability matrix")
    print("=" * 46)
    for name, ok, note in checks:
        mark = "✔" if ok else "✘"
        print(f"  {mark}  {name:24s} {note}")
    print("=" * 46)
    print("Run `python -m aura serve` to start. Demo profile works anywhere.")
    return 0


def cmd_serve(cfg) -> int:  # noqa: ANN001
    from .events import EventBus
    from .laya import ExampleBuffer, build_backend
    from .memory import Memory
    from .orchestrator import Orchestrator
    from .planner import build_planner
    from .safety import SafetyGate
    from .server import AuraServer
    from .skills import build_default_registry
    from .stt import build_stt
    from .tts import build_tts
    from pathlib import Path

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
        server.start()
        url = f"http://{'127.0.0.1' if cfg.server.host == '0.0.0.0' else cfg.server.host}:{cfg.server.port}"
        print(f"Aura v{__import__('aura', fromlist=['__version__']).__version__} "
              f"— profile={_profile_for(cfg)} bridge={bridge.platform}")
        print(f"UI:      {url}")
        print(f"Data:    {data_dir}")
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


def _build_bridge(cfg):  # noqa: ANN001
    from .skills import DryRunBridge, MacBridge

    if cfg.profile == "demo":
        return DryRunBridge()
    if sys.platform == "darwin":
        return MacBridge()
    return DryRunBridge()


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    parser = argparse.ArgumentParser(prog="aura", description="Aura — your Mac, at your command.")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "doctor"])
    parser.add_argument("--config", help="path to a config.toml")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.command == "doctor":
        return cmd_doctor(cfg)
    return cmd_serve(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
