"""Logging: the engine's answer to "what actually happened?".

Three sinks, one tree (`aura.log.setup`):

    stderr            → the app captures it into ~/Library/Logs/Aura.log
    <data>/aura.log   → rotating file, survives a crash, greppable anywhere
    the EventBus      → SSE `log` events → the app's Activity feed

And two promises this file pins down:

  * a failure names its cause (`describe_exception`) instead of saying
    "something went wrong";
  * logging never breaks the engine — a dead stream, a closed bus or an
    unwritable data directory must cost the record, never the command.
"""

from __future__ import annotations

import json
import logging

from aura import log as log_mod


class FakeBus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, type_: str, **data):
        self.events.append((type_, data))

    def logged(self) -> list[str]:
        return [d.get("line", "") for t, d in self.events if t == "log"]


class TestSetup:
    def test_writes_a_rotating_file_and_reports_it(self, tmp_path, clean_aura_logging):
        from aura.log import get_logger

        path = log_mod.setup(tmp_path, level="debug", event_level="info")
        assert path == tmp_path / "aura.log"
        get_logger("test").info("hello from the engine")
        for handler in logging.getLogger("aura").handlers:
            handler.flush()
        assert "hello from the engine" in path.read_text()
        assert log_mod.log_status()["file"] == str(path)

    def test_setup_is_idempotent(self, tmp_path, clean_aura_logging):
        log_mod.setup(tmp_path, level="info")
        handlers = len(logging.getLogger("aura").handlers)
        log_mod.setup(tmp_path, level="info")
        assert len(logging.getLogger("aura").handlers) == handlers

    def test_a_bad_data_dir_still_logs(self, tmp_path, clean_aura_logging):
        target = tmp_path / "a-file-not-a-dir"
        target.write_text("")
        log_mod.setup(target, level="info")       # must not raise
        assert log_mod.log_status()["file"] is None

    def test_levels_are_names_or_numbers(self):
        assert log_mod.level_name("warning") == "WARNING"
        assert log_mod.level_name(logging.DEBUG) == "DEBUG"
        assert log_mod.level_name("nonsense") == "INFO"


class TestBusMirroring:
    def test_records_become_activity_events(self, tmp_path, clean_aura_logging):
        from aura.log import get_logger

        bus = FakeBus()
        log_mod.setup(tmp_path, level="debug", event_level="info")
        log_mod.attach_bus(bus, "info")
        try:
            get_logger("laya").info("gate: 2 actions in 41ms")
            logged = bus.logged()
            assert any("gate: 2 actions in 41ms" in line for line in logged)
            assert any("aura.laya" in line for line in logged)
        finally:
            log_mod.detach_bus()

    def test_the_event_level_filters_the_feed(self, tmp_path, clean_aura_logging):
        from aura.log import get_logger

        log_mod.setup(tmp_path, level="debug", event_level="warning")
        bus = FakeBus()
        log_mod.attach_bus(bus, "warning")
        try:
            get_logger("test").info("routine line")
            get_logger("test").warning("something worth showing")
            logged = bus.logged()
            assert not any("routine line" in line for line in logged)
            assert any("something worth showing" in line for line in logged)
        finally:
            log_mod.detach_bus()

    def test_a_broken_bus_never_breaks_logging(self, tmp_path, clean_aura_logging):
        from aura.log import get_logger

        class Exploding:
            def publish(self, *a, **k):
                raise RuntimeError("bus is gone")

        log_mod.setup(tmp_path, level="info")
        log_mod.attach_bus(Exploding(), "info")
        try:
            get_logger("test").warning("this must not raise")
        finally:
            log_mod.detach_bus()
            log_mod.attach_bus(FakeBus(), "info")     # leave a working bus behind


class TestErrorDetail:
    def test_describe_exception_names_the_class_and_the_message(self):
        assert log_mod.describe_exception(AttributeError("'Router' has no 'ask'")) == \
            "AttributeError: 'Router' has no 'ask'"
        assert log_mod.describe_exception(ValueError()) == "ValueError: <no message>"
        assert log_mod.describe_exception(None) == "no error detail available"

    def test_describe_exception_is_bounded(self):
        detail = log_mod.describe_exception(ValueError("x" * 500), limit=40)
        assert len(detail) <= 40

    def test_log_exception_writes_the_traceback_and_returns_the_short_form(
            self, tmp_path, clean_aura_logging, aura_logs):
        log_mod.setup(tmp_path, level="debug", file=True)
        try:
            raise RuntimeError("a specific thing broke")
        except RuntimeError as exc:
            detail = log_mod.log_exception("stage failed", exc)

        assert detail == "RuntimeError: a specific thing broke"
        record = next(r for r in aura_logs if "stage failed" in r.getMessage())
        assert record.levelno == logging.ERROR
        assert "Traceback" in record.getMessage()
        assert "a specific thing broke" in record.getMessage()

    def test_the_loop_handler_records_unhandled_errors(self, aura_logs):
        exc = TypeError("bad callback")

        def boom():
            raise exc

        try:
            boom()
        except TypeError as caught:
            log_mod._loop_exception_handler(None, {"message": "task crashed", "exception": caught})
        record = next(r for r in aura_logs if "task crashed" in r.getMessage())
        assert "TypeError: bad callback" in record.getMessage()


class TestTail:
    def test_tail_reads_the_last_lines(self, tmp_path, clean_aura_logging):
        from aura.log import get_logger

        log_mod.setup(tmp_path, level="debug")
        for index in range(20):
            get_logger("test").info("line %d", index)
        for handler in logging.getLogger("aura").handlers:
            handler.flush()
        lines = log_mod.tail(5)
        assert len(lines) == 5
        assert lines[-1].endswith("line 19")

    def test_tail_is_empty_without_a_file(self, clean_aura_logging):
        log_mod.setup(None, file=False)
        assert log_mod.tail(10) == []


# --------------------------------------------------------------------------- #
# The endpoint the app and an engineer both read                               #
# --------------------------------------------------------------------------- #


def test_api_log_returns_the_engine_log(server, tmp_path, clean_aura_logging):
    """`GET /api/log` is what "Activity ▸ Open Log" and a bug report read."""
    from conftest import get

    from aura.log import get_logger

    orch, srv, cfg = server
    log_mod.setup(tmp_path, level="debug")
    get_logger("test").warning("a line a support engineer needs")
    for handler in logging.getLogger("aura").handlers:
        handler.flush()

    _status, body = get(f"http://127.0.0.1:{cfg.server.port}/api/log?tail=50")
    payload = json.loads(body)
    assert payload["lines"], "the log tail must not be empty"
    assert any("a line a support engineer needs" in line for line in payload["lines"])
    assert payload["status"]["file"].endswith("aura.log")


def test_health_and_state_report_the_decision_layer(server):
    from conftest import get

    _orch, _srv, cfg = server
    base = f"http://127.0.0.1:{cfg.server.port}"
    _status, health = get(f"{base}/api/health")
    payload = json.loads(health)
    assert "laya_ready" in payload and "laya_backend" in payload

    _status, state_body = get(f"{base}/api/state")
    state = json.loads(state_body)
    assert state["laya"]["backend"]
    assert state["laya"]["examples"]["total"] == 0
    assert state["planner"]["class"]
    assert "log_file" in state["laya"]


# --------------------------------------------------------------------------- #
# The engine's own sessions, logged end-to-end                                 #
# --------------------------------------------------------------------------- #


async def test_a_session_leaves_a_trail(monkeypatch, stack, aura_logs):
    """One command must be explainable afterwards: transcript → plan → gate →
    skill → reply, each with the numbers that matter."""
    orch = stack.build_orchestrator()
    await orch.submit_text("open spotify")
    messages = "\n".join(record.getMessage() for record in aura_logs)

    assert "session " in messages and "open spotify" in messages
    assert "plan:" in messages and "system.open_app" in messages
    assert "gate:" in messages and "match=" in messages
    assert "run: system.open_app" in messages
    assert "result: system.open_app ok" in messages
    assert "reply (ok," in messages


async def test_a_crashing_planner_returns_the_error_to_the_user(stack, aura_logs):
    """The user asked for details: an error reply must say *what* failed."""
    orch = stack.build_orchestrator()

    async def explode(transcript, context):
        raise RuntimeError("the model server is unreachable")

    orch.planner.plan = explode
    sid = orch.bus.subscribe_async()
    await orch.submit_text("open spotify")

    replies = []
    while True:
        from aura.events import EventBus  # noqa: F401  (keeps the import obvious)

        drained = orch.bus.drain(sid)
        replies.extend(ev for ev in drained if ev.type == "reply")
        if not drained:
            break
    orch.bus.unsubscribe_async(sid)

    assert replies, "the user must always get an answer"
    text = replies[-1].data["text"]
    assert "RuntimeError: the model server is unreachable" in text
    assert replies[-1].data["outcome"] == "failed"
    assert replies[-1].data["diagnostic"]
    assert any("session failed" in r.getMessage() for r in aura_logs)
    assert orch.state == "armed"


async def test_a_failing_gate_does_not_lose_the_action(stack, aura_logs):
    """A gate that raises is a bug — but the action must still be judged and
    the session must still finish."""
    orch = stack.build_orchestrator()

    def explode_many(transcript, cases):
        raise AttributeError("'Router' object has no attribute 'ask'")

    orch.laya.decide_many = explode_many
    await orch.submit_text("open spotify")
    assert orch.state == "armed"
    assert any("the Laya decision call failed" in r.getMessage() for r in aura_logs)
    # The offline scorer judged it, so the reflexive command still ran.
    assert any("Spotify" in call[1] for call in orch.bridge.calls)


async def test_the_gate_is_asked_once_per_request(stack):
    """A two-action command costs one decision call, not one per action."""
    orch = stack.build_orchestrator()
    calls = []
    original = orch.laya.decide_many

    def counting(transcript, cases):
        calls.append(list(cases))
        return original(transcript, cases)

    orch.laya.decide_many = counting
    await orch.submit_text("open spotify and set volume to 30")
    assert len(calls) == 1
    assert len(calls[0]) == 2


async def test_examples_reuse_the_session_decision(stack):
    """Supervision recording must not re-ask the model for a number the
    session already has (that was a second forward pass per action)."""
    orch = stack.build_orchestrator()
    calls = []
    original = orch.laya.decide_many

    def counting(transcript, cases):
        calls.append(list(cases))
        return original(transcript, cases)

    orch.laya.decide_many = counting
    await orch.submit_text("open spotify")
    assert len(calls) == 1
    assert orch.examples.stats()["total"] == 1
