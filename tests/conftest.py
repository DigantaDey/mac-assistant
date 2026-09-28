"""Shared fixtures: a complete demo-mode Aura stack, fast and deterministic."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aura.config import load_config
from aura.events import EventBus
from aura.laya import ExampleBuffer, HeuristicBackend
from aura.memory import Memory
from aura.orchestrator import Orchestrator
from aura.planner import MockPlanner
from aura.safety import SafetyGate
from aura.skills import DryRunBridge, build_default_registry
from aura.stt import NullSTT
from aura.tts import NullTTS


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


# --------------------------------------------------------------------------- #
# Real-HTTP helpers + a live server fixture (used by server & permissions tests)
# --------------------------------------------------------------------------- #

import json as _json
import socket as _socket
import threading as _threading
import urllib.request as _urllib


def free_port() -> int:
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str) -> tuple[int, bytes]:
    with _urllib.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def post(url: str, body: dict) -> tuple[int, dict]:
    req = _urllib.Request(
        url, data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with _urllib.urlopen(req, timeout=5) as r:
        return r.status, _json.loads(r.read())


@pytest.fixture()
def server(stack: DemoStack):
    from aura.server import AuraServer

    orch = stack.build_orchestrator()
    stack.cfg.server.port = free_port()
    srv = AuraServer(orch, stack.cfg)

    async def boot():
        await orch.start()
        srv.start()

    loop = asyncio.new_event_loop()
    started = _threading.Event()

    def run():
        loop.run_until_complete(boot())  # sets orch._loop, starts HTTP thread
        started.set()
        loop.run_forever()               # keep serving, like cmd_serve does

    _threading.Thread(target=run, daemon=True).start()
    assert started.wait(5), "orchestrator failed to boot"
    yield orch, srv, stack.cfg

    async def down():
        srv.stop()
        await orch.stop()

    asyncio.run_coroutine_threadsafe(down(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    _threading.Event().wait(0.1)


async def collect(bus: EventBus, sid: int, coro, timeout: float = 5.0) -> list[str]:
    """Run a coroutine, collecting event types it publishes until it finishes."""
    types: list[str] = []
    task = asyncio.create_task(coro)
    while not task.done():
        try:
            ev = await asyncio.wait_for(bus.get(sid), timeout=timeout)
            types.append(ev.type)
        except TimeoutError:
            break
    for ev in bus.drain(sid):
        types.append(ev.type)
    if task.exception():
        raise task.exception()
    return types
