"""Always-listening: phrase gate, phrase stripping, live wake-mode switching."""

from __future__ import annotations

import asyncio
import contextlib
import time

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

    async def test_switch_refuses_to_claim_listening_without_a_live_detector(
        self, tmp_path, monkeypatch,
    ):
        stack = self.build_mac_stack(tmp_path, monkeypatch)
        orch = stack.build_orchestrator()
        orch._has_audio = True
        orch._audio_task = asyncio.current_task()

        from aura.wakeword import ManualTrigger

        def fallback(_cfg, on_fallback=None):
            if on_fallback:
                on_fallback("test detector unavailable")
            return ManualTrigger()

        monkeypatch.setattr("aura.wakeword.build_wake_engine", fallback)
        result = await orch.set_wake_mode("openwakeword", phrase="hey aura")

        assert result["ok"] is False
        assert "couldn't start" in result["message"].lower()
        assert stack.cfg.wake.mode == "manual"
        assert orch.wake_status()["active"] is False

    async def test_switch_refuses_when_audio_listener_is_not_running(
        self, tmp_path, monkeypatch,
    ):
        stack = self.build_mac_stack(tmp_path, monkeypatch)
        orch = stack.build_orchestrator()
        orch._has_audio = True
        result = await orch.set_wake_mode("openwakeword", phrase="hey aura")
        assert result["ok"] is False
        assert "listener is not running" in result["message"].lower()
        assert stack.cfg.wake.mode == "manual"

    async def test_switch_persists_only_after_a_real_detector_is_built(
        self, tmp_path, monkeypatch,
    ):
        stack = self.build_mac_stack(tmp_path, monkeypatch)
        orch = stack.build_orchestrator()
        orch._has_audio = True
        orch._audio_task = asyncio.current_task()

        from aura.wakeword import WakeEngine

        class FakeWake(WakeEngine):
            pass

        orch._build_wake = lambda: FakeWake()
        result = await orch.set_wake_mode("openwakeword", phrase="hey aura")

        assert result["ok"] is True
        assert result["phrase"] == "hey aura"
        assert result["wake_active"] is True
        from aura.config import load_config
        cfg = load_config()
        assert cfg.wake.mode == "openwakeword"
        assert cfg.wake.phrase == "hey aura"

    async def test_wake_engine_frame_activates_capture(self, stack: DemoStack):
        from aura.audio import AudioFrame
        from aura.wakeword import WakeEngine

        class OneShotWake(WakeEngine):
            def feed(self, _frame):
                return True

        orch = stack.build_orchestrator()
        orch.cfg.wake.mode = "openwakeword"
        orch._wake = OneShotWake()
        orch._has_audio = True
        listener = asyncio.create_task(orch._audio_loop())
        orch._audio_task = listener
        try:
            await orch._queue.put(AudioFrame(pcm=b"\0" * 640, ts=time.time()))
            for _ in range(100):
                if orch.state == "capturing":
                    break
                await asyncio.sleep(0.001)
            assert orch.state == "capturing"
            assert orch.wake_status()["active"] is True
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    async def test_detector_runtime_failure_degrades_without_killing_listener(
        self, stack: DemoStack,
    ):
        from aura.audio import AudioFrame
        from aura.wakeword import ManualTrigger, WakeEngine

        class BrokenWake(WakeEngine):
            def feed(self, _frame):
                raise RuntimeError("inference crashed")

        orch = stack.build_orchestrator()
        orch.cfg.wake.mode = "openwakeword"
        orch._wake = BrokenWake()
        orch._has_audio = True
        listener = asyncio.create_task(orch._audio_loop())
        orch._audio_task = listener
        try:
            await orch._queue.put(AudioFrame(pcm=b"\0" * 640, ts=time.time()))
            for _ in range(100):
                if isinstance(orch._wake, ManualTrigger):
                    break
                await asyncio.sleep(0.001)
            assert isinstance(orch._wake, ManualTrigger)
            assert listener.done() is False
            assert orch.wake_status()["active"] is False
            assert "inference crashed" in orch.wake_status()["detail"]
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

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
