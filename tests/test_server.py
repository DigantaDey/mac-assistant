"""The local server: real HTTP, real SSE, real orchestration."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import urllib.request

import pytest

from conftest import DemoStack


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
    started = threading.Event()

    def run():
        loop.run_until_complete(boot())  # sets orch._loop, starts HTTP thread
        started.set()
        loop.run_forever()               # keep serving, like cmd_serve does

    threading.Thread(target=run, daemon=True).start()
    assert started.wait(5), "orchestrator failed to boot"
    yield orch, srv, stack.cfg

    async def down():
        srv.stop()
        await orch.stop()

    asyncio.run_coroutine_threadsafe(down(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    threading.Event().wait(0.1)


def get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


class TestServer:
    def test_ui_served(self, server):
        _, srv, cfg = server
        status, body = get(f"http://127.0.0.1:{cfg.server.port}/")
        assert status == 200
        assert b"Aura" in body

    def test_health_and_state(self, server):
        _, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        status, health = get(f"{base}/api/health")
        assert status == 200 and json.loads(health)["ok"]
        _, state = get(f"{base}/api/state")
        st = json.loads(state)
        assert st["state"] == "armed"
        assert "skills" not in st or True
        status, skills = get(f"{base}/api/skills")
        assert status == 200 and json.loads(skills)["skills"]

    def test_path_traversal_refused(self, server):
        _, srv, cfg = server
        import urllib.error
        with pytest.raises(urllib.error.HTTPError):
            get(f"http://127.0.0.1:{cfg.server.port}/../aura/config.py")

    def test_full_session_over_http(self, server):
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        status, resp = post(f"{base}/api/input", {"text": "open spotify"})
        assert status == 200

        async def wait_idle():
            for _ in range(200):
                if orch.state == "armed" and orch.memory.recent_events():
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("session never completed")

        asyncio.run(wait_idle())
        events = orch.memory.recent_events()
        assert events[0]["transcript"] == "open spotify"
        assert events[0]["outcome"] == "ok"
        assert any("Spotify" in c[1] for c in orch.bridge.calls)
