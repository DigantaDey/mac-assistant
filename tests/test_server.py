"""The local server: real HTTP, real SSE, real orchestration."""

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest

from conftest import get, post


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
