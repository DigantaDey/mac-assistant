"""The local server: real HTTP, real SSE, real orchestration."""

from __future__ import annotations

import asyncio
import http.client
import json
import urllib.error

import pytest
from conftest import auth_headers, get, post


def _open_event_stream(port: int, extra_headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", "/api/events", headers=auth_headers(extra_headers))
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert response.fp.readline() == b"retry: 2000\n"
    assert response.fp.readline() == b"\n"
    return connection, response


def _read_sse_event(response) -> dict:
    event_id = None
    payload = None
    while True:
        line = response.fp.readline()
        if not line:
            raise AssertionError("event stream closed before the expected event")
        if line.startswith(b"id: "):
            event_id = int(line[4:].strip())
        elif line.startswith(b"data: "):
            payload = json.loads(line[6:])
        elif line == b"\n" and event_id is not None and payload is not None:
            return {"id": event_id, **payload}


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
        assert st["wake_mode"] == "manual"
        assert st["wake_active"] is False
        assert st["wake_engine"] == "ManualTrigger"
        status, skills = get(f"{base}/api/skills")
        assert status == 200 and json.loads(skills)["skills"]

    def test_event_stream_resumes_and_resets_cursor_on_engine_restart(self, server):
        orch, _, cfg = server
        port = cfg.server.port
        first_connection, first_response = _open_event_stream(port)
        try:
            epoch = first_response.getheader("X-Aura-Event-Epoch")
            assert epoch == orch.bus.epoch
            live = orch.bus.publish("state", state="planning")
            first = _read_sse_event(first_response)
            assert first["id"] == live.seq
            assert first["type"] == "state"
        finally:
            first_response.close()
            first_connection.close()

        missed = orch.bus.publish("reply", text="replayed after reconnect")
        resumed_connection, resumed_response = _open_event_stream(
            port, {"Last-Event-ID": str(live.seq), "X-Aura-Event-Epoch": epoch})
        try:
            replay = _read_sse_event(resumed_response)
            assert replay["id"] == missed.seq
            assert replay["data"]["text"] == "replayed after reconnect"
        finally:
            resumed_response.close()
            resumed_connection.close()

        # The last cursor may be larger than the sequence counter after the
        # engine process restarts. A changed epoch replays this process's
        # bounded history instead of waiting for the old cursor to be reached.
        after_restart = orch.bus.publish("reply", text="new engine history")
        restarted_connection, restarted_response = _open_event_stream(
            port, {"Last-Event-ID": "999999", "X-Aura-Event-Epoch": "old-engine"})
        try:
            assert restarted_response.getheader("X-Aura-Event-Epoch") == epoch
            replayed = []
            while not any(event["id"] == after_restart.seq for event in replayed):
                replayed.append(_read_sse_event(restarted_response))
            assert replayed[-1]["data"]["text"] == "new engine history"
        finally:
            restarted_response.close()
            restarted_connection.close()
            # Wake the handler out of its blocking queue wait so it notices
            # the closed socket and unsubscribes immediately.
            orch.bus.publish("log", line="SSE test disconnect")

    def test_path_traversal_refused(self, server):
        _, srv, cfg = server
        with pytest.raises(urllib.error.HTTPError):
            get(f"http://127.0.0.1:{cfg.server.port}/../aura/config.py")

    def test_port_in_use_raises_ostypes_cleanly(self, server):
        """A second server on the same port fails with a clean OSError —
        cmd_serve turns that into a friendly message, not a traceback."""
        orch, srv, cfg = server
        from aura.server import AuraServer

        with pytest.raises(OSError):
            AuraServer(orch, cfg).start()

    def test_state_exposes_laya_confidence(self, server):
        _, srv, cfg = server
        _, state = get(f"http://127.0.0.1:{cfg.server.port}/api/state")
        st = json.loads(state)
        assert st["laya"]["confidence"] == cfg.laya.confidence_threshold

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
    # "It's been thinking for ten minutes" — the input path must never     #
    # tear a live session down. The HTTP answer is sent before the session #
    # runs, so there is nobody left to time out; the orchestrator's        #
    # watchdog is what bounds the work.                                    #
    # ------------------------------------------------------------------ #

    def test_slow_command_still_completes(self, server):
        """A command slower than the old 30 s fire-and-forget cap still runs
        to the end, and the next command is accepted."""
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"

        class SlowPlanner:
            async def plan(self, transcript, context):
                await asyncio.sleep(0.4)
                from aura.planner import Action, Plan

                return Plan(reply="Opened it.",
                            actions=[Action("system.open_app", {"app": "Spotify"})])

        orch.planner = SlowPlanner()
        status, resp = post(f"{base}/api/input", {"text": "open spotify"})
        assert status == 200 and resp["accepted"] is True

        async def wait_idle():
            for _ in range(300):
                if orch.state == "armed" and orch.memory.recent_events():
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("a slow command must still complete")

        asyncio.run(wait_idle())
        assert any("Spotify" in c[1] for c in orch.bridge.calls)

        # and Aura takes the next command straight away
        status, resp = post(f"{base}/api/input", {"text": "set volume to 30"})
        assert resp["accepted"] is True

    def test_wedged_session_ends_with_an_answer(self, server):
        """Whatever goes wrong, the engine comes back to Ready and says so —
        it never sits in 'thinking' forever."""
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        orch.cfg.session.max_session_seconds = 0.3
        orch.cfg.session.confirmation_timeout_seconds = 0.2

        class HangingPlanner:
            async def plan(self, transcript, context):
                await asyncio.sleep(30)

        orch.planner = HangingPlanner()
        post(f"{base}/api/input", {"text": "open spotify"})

        async def wait_idle():
            for _ in range(300):
                if orch.state == "armed" and orch.memory.recent_events():
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("the session never came back")

        asyncio.run(wait_idle())
        assert orch.memory.recent_events() or True   # ended, one way or another
        status, resp = post(f"{base}/api/input", {"text": "set volume to 30"})
        assert resp["accepted"] is True

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

    def test_training_state_transitions_are_serialized_with_audio_loop(self, server):
        orch, _srv, cfg = server
        orch._has_audio = True
        engine_loop = orch.loop
        observed = []
        original_start = orch.training_start
        original_capture = orch.training_capture

        def check_loop(callback):
            def wrapped(*args):
                observed.append(asyncio.get_running_loop() is engine_loop)
                return callback(*args)
            return wrapped

        orch.training_start = check_loop(original_start)
        orch.training_capture = check_loop(original_capture)
        base = f"http://127.0.0.1:{cfg.server.port}"
        _, started = post(f"{base}/api/wake/train", {"phrase": "hey aura"})
        assert started["ok"] is True
        _, captured = post(f"{base}/api/wake/train/capture", {})
        assert captured["ok"] is True
        assert observed == [True, True]
        post(f"{base}/api/wake/train/cancel", {})

    def test_setup_install_refused_in_demo(self, server):
        _, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        _, res = post(f"{base}/api/setup/install", {})
        assert res["ok"] is False


class TestSetupAndFeedbackContracts:
    def test_test_automation_reply_decodes_by_the_app_contract(self, server):
        """The app decodes `EngineReply` with a required `ok`; an answer
        without it used to die silently inside `try?` — a dead button."""
        orch, srv, cfg = server
        status, body = post(
            f"http://127.0.0.1:{cfg.server.port}/api/permissions/test_automation", {})
        assert status == 200
        assert "ok" in body and "status" in body and "message" in body
        assert body["ok"] is (body["status"] == "ok")

    def test_feedback_is_answered_fast_and_recorded_with_its_skill(self, server):
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        import time as _time

        started = _time.monotonic()
        status, body = post(f"{base}/api/correct",
                            {"transcript": "open youtube.com in safari",
                             "skill": "browser.open_url", "verdict": "bad",
                             "args": {"url": "youtube.com", "browser": "Safari"}})
        assert status == 200 and body["ok"] is True
        assert _time.monotonic() - started < 2.0, "a thumb tap must not block on the model"

        async def wait_recorded():
            for _ in range(200):
                if orch.examples.stats()["corrected"]:
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("feedback example never landed")

        asyncio.run(wait_recorded())
        assert orch.examples.stats()["corrected"] == 1

    def test_feedback_without_a_skill_records_no_bogus_example(self, server):
        orch, srv, cfg = server
        status, body = post(f"http://127.0.0.1:{cfg.server.port}/api/correct",
                            {"transcript": "open spotify", "skill": "",
                             "verdict": "bad"})
        assert status == 200 and body["ok"] is True

        async def settle():
            await asyncio.sleep(0.2)

        asyncio.run(settle())
        assert orch.examples.stats()["total"] == 0

    def test_history_carries_the_plan_so_feedback_has_a_skill(self, server):
        orch, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        post(f"{base}/api/input", {"text": "open spotify"})

        async def wait_idle():
            for _ in range(200):
                if orch.state == "armed" and orch.memory.recent_events():
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("session never completed")

        asyncio.run(wait_idle())
        status, body = get(f"{base}/api/history")
        assert status == 200
        events = json.loads(body)["events"]
        assert events and events[0]["plan"]["actions"]
        assert events[0]["plan"]["actions"][0]["skill"] == "system.open_app"
