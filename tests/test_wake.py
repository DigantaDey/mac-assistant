"""Always-listening: phrase gate, phrase stripping, live wake-mode switching."""

from __future__ import annotations

from conftest import DemoStack

from aura.wakeword import phrase_gate, strip_phrase


class TestPhraseGate:
    def test_empty_phrase_never_gates(self):
        assert phrase_gate("open spotify", "") is True

    def test_exact_prefix(self):
        assert phrase_gate("hey aura open spotify", "hey aura") is True

    def test_case_and_punctuation_lenient(self):
        assert phrase_gate("Hey, Aura! set volume to 30", "hey aura") is True

    def test_missing_phrase_rejected(self):
        assert phrase_gate("open spotify", "hey aura") is False

    def test_phrase_must_lead(self):
        assert phrase_gate("open spotify hey aura", "hey aura") is False

    def test_similar_but_wrong_rejected(self):
        assert phrase_gate("hey auraa open spotify", "hey aura") is False


class TestStripPhrase:
    def test_strips_leading_phrase(self):
        rest = strip_phrase("hey aura open spotify", "hey aura")
        assert "spotify" in rest.lower()
        assert "hey aura" not in rest.lower()

    def test_noop_when_absent(self):
        assert strip_phrase("open spotify", "hey aura") == "open spotify"

    def test_noop_without_phrase(self):
        assert strip_phrase("open spotify", "") == "open spotify"

    def test_phrase_only(self):
        assert strip_phrase("hey aura", "hey aura") == ""


class TestLiveWakeSwitch:
    def build_mac_stack(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path))
        stack = DemoStack(tmp_path)
        stack.cfg.profile = "mac"          # not pinned to manual like demo
        return stack

    async def test_switch_persists_and_rebuilds_engine(self, tmp_path, monkeypatch):
        stack = self.build_mac_stack(tmp_path, monkeypatch)
        orch = stack.build_orchestrator()
        await orch.start()

        result = await orch.set_wake_mode("openwakeword", phrase="hey aura")
        await orch.stop()

        assert result["ok"] is True
        assert result["phrase"] == "hey aura"
        # openwakeword isn't importable on CI → engine falls back, honestly
        assert result["engine"] in ("OpenWakeWordEngine", "ManualTrigger")

        # the choice survives a restart
        from aura.config import load_config
        cfg = load_config()
        assert cfg.wake.mode == "openwakeword"
        assert cfg.wake.phrase == "hey aura"

    async def test_unknown_mode_refused(self, tmp_path, monkeypatch):
        stack = self.build_mac_stack(tmp_path, monkeypatch)
        orch = stack.build_orchestrator()
        result = await orch.set_wake_mode("clap_twice")
        assert result["ok"] is False

    async def test_demo_profile_refuses(self, stack: DemoStack):
        orch = stack.build_orchestrator()
        result = await orch.set_wake_mode("openwakeword")
        assert result["ok"] is False
        assert "demo" in result["message"].lower()

    async def test_over_http(self, server):
        orch, srv, cfg = server
        from conftest import post

        status, data = post(f"http://127.0.0.1:{cfg.server.port}/api/wake",
                            {"mode": "openwakeword"})
        assert status == 200
        assert data["ok"] is False        # demo profile refuses — honest
        assert "demo" in data["message"].lower()


class TestIdleUnload:
    async def test_unload_called_after_idle(self, stack: DemoStack):
        orch = stack.build_orchestrator()
        unloaded = []

        class FakeSTT:
            def transcribe(self, frames):
                return ""

            def unload(self):
                unloaded.append(True)
                return True

        orch.stt = FakeSTT()
        orch._last_activity -= stack.cfg.session.idle_unload_seconds + 1
        orch._unload_if_idle()
        assert unloaded == [True]

    async def test_no_unload_when_busy_recently(self, stack: DemoStack):
        orch = stack.build_orchestrator()
        calls = []

        class FakeSTT:
            def transcribe(self, frames):
                return ""

            def unload(self):
                calls.append(1)
                return True

        orch.stt = FakeSTT()
        orch._unload_if_idle()            # activity is recent
        assert calls == []
