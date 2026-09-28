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
        status, skills = get(f"{base}/api/skills")
        assert status == 200 and json.loads(skills)["skills"]

    def test_path_traversal_refused(self, server):
        _, srv, cfg = server
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

    # ------------------------------------------------------------------ #
    # Wake Phrase Studio + in-app installer over HTTP                      #
    # ------------------------------------------------------------------ #

    def test_training_endpoints_honest_without_mic(self, server):
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        _, status = get(f"{base}/api/wake/train")
        assert json.loads(status) == {"active": False}

        _, res = post(f"{base}/api/wake/train", {"phrase": "hey aura"})
        assert res["ok"] is False
        assert "microphone" in res["message"].lower()

    def test_training_endpoints_with_mic(self, server):
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"

        _, bad = post(f"{base}/api/wake/train", {"phrase": "way too many words here ok"})
        assert bad["ok"] is False

        orch._has_audio = True  # simulate granted microphone
        _, res = post(f"{base}/api/wake/train", {"phrase": "Hey Aura"})
        assert res["ok"] is True and res["need"] == 6

        _, status = get(f"{base}/api/wake/train")
        st = json.loads(status)
        assert st["active"] and st["phrase"] == "hey aura" and st["count"] == 0

        _, cap = post(f"{base}/api/wake/train/capture", {})
        assert cap["ok"] is True

        _, cancel = post(f"{base}/api/wake/train/cancel", {})
        assert cancel["ok"] is True
        _, status = get(f"{base}/api/wake/train")
        assert json.loads(status) == {"active": False}

    def test_setup_install_refused_in_demo(self, server):
        _, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        _, res = post(f"{base}/api/setup/install", {})
        assert res["ok"] is False
