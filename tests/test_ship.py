"""v0.5 ship-quality tests: the bugs that made Aura feel broken.

Covers:
  * HybridPlanner — LLM unreachable ⇒ honest basic mode, never silence
  * sessions always end — an exploding planner must not freeze the orb
  * wake engine never crashes the orchestrator on missing model files
  * the new server endpoints (live settings, permission requests, steps)
  * demo-profile layering of the auto config
"""

from __future__ import annotations

import asyncio

from conftest import DemoStack, auth_headers, collect, get, post

# --------------------------------------------------------------------------- #
# HybridPlanner: the brain with a spine                                        #
# --------------------------------------------------------------------------- #


class TestHybridPlanner:
    def make_cfg(self, monkeypatch, tmp_path):
        cfg = DemoStack(tmp_path).cfg
        cfg.profile = "mac"
        cfg.planner.engine = "openai_compat"
        # Port 9 is "discard" — guaranteed closed on a normal machine.
        cfg.planner.base_url = "http://127.0.0.1:9/v1"
        return cfg

    def test_factory_gives_hybrid_on_real_engine(self, monkeypatch, tmp_path):
        from aura.planner import HybridPlanner, build_planner

        cfg = self.make_cfg(monkeypatch, tmp_path)
        from aura.skills import build_default_registry

        reg = build_default_registry()
        planner = build_planner(cfg, reg.catalog_prompt())
        assert isinstance(planner, HybridPlanner)

    def test_dead_endpoint_degrades_to_basic_mode(self, monkeypatch, tmp_path):
        from aura.planner import build_planner
        from aura.skills import build_default_registry

        cfg = self.make_cfg(monkeypatch, tmp_path)
        reg = build_default_registry()
        planner = build_planner(cfg, reg.catalog_prompt())

        assert planner.probe() is False
        plan = asyncio.run(planner.plan("open spotify", {}))
        assert plan.degraded is True
        assert plan.actions and plan.actions[0].skill == "system.open_app"
        assert planner.status["online"] is False

    def test_mock_planner_is_never_degraded(self, tmp_path):
        from aura.planner import build_planner
        from aura.skills import build_default_registry

        cfg = DemoStack(tmp_path).cfg
        reg = build_default_registry()
        planner = build_planner(cfg, reg.catalog_prompt())
        plan = asyncio.run(planner.plan("open spotify", {}))
        assert plan.degraded is False


# --------------------------------------------------------------------------- #
# A session must always end — never a frozen orb                               #
# --------------------------------------------------------------------------- #


class _ExplodingPlanner:
    async def plan(self, transcript, context):
        raise RuntimeError("the model server vanished mid-thought")


async def test_exploding_planner_still_answers(stack: DemoStack):
    orch = stack.build_orchestrator()
    orch.planner = _ExplodingPlanner()
    await orch.start()
    try:
        sid = orch.bus.subscribe_async()
        types = await collect(orch.bus, sid, orch.submit_text("open spotify"))
        assert orch.state == "armed", "the session must end, whatever happened"
        assert "reply" in types
        orch.bus.unsubscribe_async(sid)
    finally:
        await orch.stop()


# --------------------------------------------------------------------------- #
# Wake engine: fall back, never crash                                          #
# --------------------------------------------------------------------------- #


class TestWakeFallback:
    def test_missing_model_file_falls_back(self, tmp_path):
        from aura.wakeword import ManualTrigger, build_wake_engine

        cfg = DemoStack(tmp_path).cfg
        cfg.wake.mode = "openwakeword"
        cfg.wake.models = [str(tmp_path / "nope.onnx")]
        notes: list[str] = []
        engine = build_wake_engine(cfg, on_fallback=notes.append)
        assert isinstance(engine, ManualTrigger)
        assert notes and "manual wake" in notes[0]

    def test_models_ready_is_honest(self, tmp_path):
        from aura.wakeword import wake_models_ready

        cfg = DemoStack(tmp_path).cfg
        cfg.wake.models = [str(tmp_path / "missing.npz")]
        ready, detail = wake_models_ready(cfg)
        assert ready is False and "missing.npz" in detail

        present = tmp_path / "phrase.npz"
        present.write_bytes(b"x")
        cfg.wake.models = [str(present)]
        ready, _ = wake_models_ready(cfg)
        assert ready is True

    def test_orchestrator_start_survives_bad_wake_config(self, tmp_path):
        from aura.wakeword import ManualTrigger

        stack = DemoStack(tmp_path)
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(tmp_path / "absent.onnx")]
        orch = stack.build_orchestrator()

        async def run():
            await orch.start()
            engine = orch._wake
            await orch.stop()
            return engine

        engine = asyncio.run(run())
        assert isinstance(engine, ManualTrigger)


# --------------------------------------------------------------------------- #
# New server endpoints                                                         #
# --------------------------------------------------------------------------- #


class TestNewEndpoints:
    def test_permissions_snapshot_shape(self, server):
        orch, srv, cfg = server
        code, body = get(f"http://127.0.0.1:{cfg.server.port}/api/permissions")
        import json

        data = json.loads(body)
        assert code == 200
        assert "wake_models" in data and "ready" in data["wake_models"]

    def test_state_snapshot_shape(self, server):
        orch, srv, cfg = server
        import json

        code, body = get(f"http://127.0.0.1:{cfg.server.port}/api/state")
        data = json.loads(body)
        assert code == 200
        for key in ("planner_online", "ask_before_run", "data_dir", "mic_ready"):
            assert key in data, f"missing {key} in /api/state"

    def test_config_endpoint_roundtrip(self, server):
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        code, data = post(base + "/api/config", {"updates": {"tts": {"enabled": False}}})
        assert code == 200 and data["ok"] is True
        assert data["applied"] == ["tts.enabled"]
        code, body = get(base + "/api/state")
        import json

        assert json.loads(body)["tts_enabled"] is False
        # restored
        post(base + "/api/config", {"updates": {"tts": {"enabled": True}}})

    def test_config_endpoint_refuses_unknowns(self, server):
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        code, data = post(base + "/api/config", {"updates": {"bogus": {"x": 1}}})
        assert data["ok"] is False
        code, data = post(base + "/api/config", {"updates": {"tts": {"nope": 1}}})
        assert data["ok"] is False
        code, data = post(base + "/api/config", {"updates": {"tts": {"enabled": "yes"}}})
        assert data["ok"] is False and "on/off" in data["message"]

    def test_permissions_request_reports_honest_status(self, server):
        import json
        import urllib.error
        import urllib.request

        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        code, data = post(base + "/api/permissions/request", {"target": "microphone"})
        assert code == 200
        assert data["status"] in ("ok", "denied", "unavailable", "asked")
        req = urllib.request.Request(
            base + "/api/permissions/request",
            data=json.dumps({"target": "bogus"}).encode(),
            headers=auth_headers({"Content-Type": "application/json"}), method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("bogus target should be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    def test_setup_step_on_demo_profile(self, server):
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        code, data = post(base + "/api/setup/step", {"step": "whisper"})
        assert code == 200 and data["status"] in ("ok", "skip", "fail")

    def test_system_open_non_mac(self, server):
        import json
        import urllib.error
        import urllib.request

        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        code, data = post(base + "/api/system/open", {"what": "data"})
        assert code == 200
        assert data["ok"] in (True, False)  # honest either way

        req = urllib.request.Request(
            base + "/api/system/open",
            data=json.dumps({"what": "bogus"}).encode(),
            headers=auth_headers({"Content-Type": "application/json"}), method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("bogus target should be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400


# --------------------------------------------------------------------------- #
# The regression that made Aura "do nothing": confirm over real HTTP          #
# --------------------------------------------------------------------------- #


def test_confirm_over_http_releases_the_session(server):
    """A user taps Run from the browser; the POST lands on a *server* thread.
    The future must be woken on the orchestrator loop or the orb hangs in
    `proposing` forever."""
    import json
    import time
    import urllib.request

    orch, srv, cfg = server
    base = f"http://127.0.0.1:{cfg.server.port}"

    def post_json(path, body):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(),
            headers=auth_headers({"Content-Type": "application/json"}), method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    post_json("/api/input", {"text": "empty the trash"})
    token = None
    for _ in range(40):
        state_req = urllib.request.Request(base + "/api/state", headers=auth_headers())
        with urllib.request.urlopen(state_req, timeout=5) as r:
            st = json.loads(r.read())
        if st["state"] == "proposing" and st["session"]:
            token = st["session"]["token"]
            break
        time.sleep(0.1)
    assert token, "never reached the proposal"

    assert post_json("/api/confirm", {"token": token})["ok"] is True

    for _ in range(60):
        state_req = urllib.request.Request(base + "/api/state", headers=auth_headers())
        with urllib.request.urlopen(state_req, timeout=5) as r:
            st = json.loads(r.read())
        if st["state"] == "armed":
            break
        time.sleep(0.1)
    assert st["state"] == "armed", "confirm must release the session"
    # the dry-run bridge actually ran the destructive skill
    assert any("trash" in c[1].lower() for c in orch.bridge.calls)


# --------------------------------------------------------------------------- #
# Config layering: auto off-Mac is a demo base, not a wall                     #
# --------------------------------------------------------------------------- #


def test_auto_off_mac_pins_demo_base(monkeypatch, tmp_path):
    import sys

    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path))
    from aura.config import load_config

    cfg = load_config()
    if sys.platform != "darwin":
        assert cfg.planner.engine == "mock"
        assert cfg.stt.engine == "null"
        assert cfg.wake.mode == "manual"


def test_user_file_wins_over_demo_base(monkeypatch, tmp_path):
    """Explicit user intent (a file) beats the demo base layer."""
    import sys

    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path))
    (tmp_path / "runtime.toml").parent.mkdir(parents=True, exist_ok=True)
    # runtime.toml is the UI's persistence path — user intent.
    (tmp_path / "runtime.toml").write_text('[wake]\nmode = "openwakeword"\n')
    from aura.config import load_config

    cfg = load_config()
    if sys.platform != "darwin":
        assert cfg.wake.mode == "openwakeword"
