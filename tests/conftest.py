"""Shared fixtures: a complete demo-mode Aura stack, fast and deterministic."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aura.config import load_config  # noqa: E402
from aura.events import EventBus  # noqa: E402
from aura.laya import ExampleBuffer, HeuristicBackend  # noqa: E402
from aura.memory import Memory  # noqa: E402
from aura.orchestrator import Orchestrator  # noqa: E402
from aura.planner import MockPlanner  # noqa: E402
from aura.safety import SafetyGate  # noqa: E402
from aura.skills import DryRunBridge, build_default_registry  # noqa: E402
from aura.stt import NullSTT  # noqa: E402
from aura.tts import NullTTS  # noqa: E402


class DemoStack:
    """Everything wired the way cmd_serve wires it, but mock and in-memory."""

    def __init__(self, tmp: Path) -> None:
        cfg = load_config()
        cfg.profile = "demo"
        cfg.data_dir = str(tmp)
        self.cfg = cfg
        self.bus = EventBus()
        self.bridge = DryRunBridge()
        self.registry = build_default_registry(self.bridge)
        self.planner = MockPlanner(self.registry.catalog_prompt())
        self.laya = HeuristicBackend()
        self.safety = SafetyGate(cfg, self.laya)
        self.memory = Memory(tmp / "aura.sqlite3")
        self.examples = ExampleBuffer(tmp / "aura.sqlite3")

    def build_orchestrator(self) -> Orchestrator:
        return Orchestrator(
            cfg=self.cfg, bus=self.bus, registry=self.registry, bridge=self.bridge,
            planner=self.planner, laya_backend=self.laya, safety=self.safety,
            memory=self.memory, examples=self.examples, stt=NullSTT(), tts=NullTTS(),
        )


@pytest.fixture()
def stack(tmp_path: Path) -> DemoStack:
    return DemoStack(tmp_path)


async def collect(bus: EventBus, sid: int, coro, timeout: float = 5.0) -> list[str]:
    """Run a coroutine, collecting event types it publishes until it finishes."""
    types: list[str] = []
    task = asyncio.create_task(coro)
    while not task.done():
        try:
            ev = await asyncio.wait_for(bus.get(sid), timeout=timeout)
            types.append(ev.type)
        except asyncio.TimeoutError:
            break
    for ev in bus.drain(sid):
        types.append(ev.type)
    if task.exception():
        raise task.exception()
    return types
