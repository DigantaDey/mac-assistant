"""The real Laya adapter, tested against the API the `laya` package ships.

`aura/laya.py` used to ask the model questions through an API that never
existed (`router.ask(...)`), which meant that with `laya` installed every
single command died — and with it not installed the fallback hid the fact
entirely. These tests pin the adapter to the published surface and prove the
failure paths are visible:

    Router(preload=…)  ·  predict(state, questions)  ·  predict_batch  ·
    noul / choice / score answers  ·  routing metadata  ·  loaded()

Everything runs against `tests/fake_laya.py`, so there is no torch, no
download and no flakiness — but the *real* code path is what executes.
"""

from __future__ import annotations

import logging

import fake_laya
import pytest

from aura import laya as laya_mod
from aura.laya import (
    GATE_QUESTIONS,
    HeuristicBackend,
    LayaError,
    LayaGate,
    RealLayaBackend,
    build_backend,
    laya_available,
)


class Cfg:
    class laya:
        enabled = True
        backend = "auto"
        device = ""
        model = ""
        preload = True
        max_loaded = 2
        call_budget_seconds = 1.5
        adapter_dir = ""


@pytest.fixture()
def real(monkeypatch):
    fake_laya.install(monkeypatch)
    return RealLayaBackend()


# --------------------------------------------------------------------------- #
# The published API, used correctly                                            #
# --------------------------------------------------------------------------- #


class TestRealBackendContract:
    def test_router_is_built_with_the_published_options(self, real):
        real.load()
        router = real.status()
        assert real.ready is True
        assert router["version"] == fake_laya.VERSION
        assert router["loaded"] == ["english"]

    def test_decide_asks_both_questions_in_one_forward_pass(self, real):
        decision = real.decide("empty the trash", "system.empty_trash", {}, "permanently deletes")
        router = fake_laya.FakeRouter.instances[0]
        assert len(router.calls) == 1, "both gate questions must share one predict() call"
        _state, questions = router.calls[0]
        assert set(questions) == {"match", "destructive"}
        assert questions["match"] == GATE_QUESTIONS["match"]
        assert questions["match"]["type"] == "noul"
        assert decision.match == pytest.approx(0.9)
        assert decision.destructive == pytest.approx(0.95)
        assert decision.backend == "laya"
        assert decision.source == "real"
        assert decision.model == "english"
        assert "default" in decision.routing

    def test_decide_many_batches_the_actions_of_one_request(self, real):
        decisions = real.decide_many("open spotify and set volume to 30", [
            ("system.open_app", {"app": "Spotify"}, "asked to open it"),
            ("system.set_volume", {"level": 30}, "asked to change volume"),
        ])
        router = fake_laya.FakeRouter.instances[0]
        assert len(router.batches) == 1, "two actions must cost one batched pass"
        assert router.calls == []
        assert len(decisions) == 2
        assert decisions[0].match > 0.5 and decisions[1].match > 0.5

    def test_missing_predict_batch_still_works(self, real, monkeypatch):
        """An older/newer package without the batch call must degrade, not break."""
        monkeypatch.delattr(fake_laya.FakeRouter, "predict_batch", raising=False)
        decisions = real.decide_many("open spotify", [("system.open_app", {"app": "Spotify"}, "")])
        assert len(decisions) == 1
        assert fake_laya.FakeRouter.instances[0].calls

    def test_choose_reads_the_choice_answer(self, real):
        fake_laya.FakeRouter.picks["quiet the house"] = "system.toggle_dnd"
        choice = real.choose({"request": "quiet the house"},
                             "Which skill carries out the request?",
                             {"system.toggle_dnd": "focus mode", "system.mute": "mute"})
        assert choice.choice == "system.toggle_dnd"
        assert choice.confidence == pytest.approx(0.91)
        assert choice.probabilities["system.mute"] == pytest.approx(0.09)
        assert choice.source == "real"

    def test_score_reads_the_score_answer(self, real):
        score = real.score({"request": "set the volume to 30"},
                           "What volume level?", [str(n) for n in range(0, 101, 10)])
        assert score.index == pytest.approx(3.0)
        assert score.value == pytest.approx(3 / 10)

    def test_unexpected_result_shape_raises_laya_error(self, real, monkeypatch):
        monkeypatch.setattr(fake_laya.FakeRouter, "predict",
                            lambda self, state, questions, **kw: {"nope": 1})
        with pytest.raises(LayaError) as excinfo:
            real.decide("open spotify", "system.open_app", {"app": "Spotify"})
        assert "unexpected result shape" in str(excinfo.value)
        assert "laya predict" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Failure is loud, never silent                                               #
# --------------------------------------------------------------------------- #


class TestFailuresAreVisible:
    def test_predict_failure_names_the_stage(self, real, monkeypatch):
        fake_laya.FakeRouter.raise_on_predict = "CUDA out of memory"
        with pytest.raises(LayaError) as excinfo:
            real.decide("open spotify", "system.open_app", {"app": "Spotify"})
        assert "laya predict" in str(excinfo.value)
        assert "CUDA out of memory" in str(excinfo.value)

    def test_broken_load_is_a_laya_error(self, monkeypatch):
        fake_laya.install(monkeypatch)
        fake_laya.FakeRouter.fail_load = "no such checkpoint"
        backend = RealLayaBackend()
        with pytest.raises(LayaError) as excinfo:
            backend.load()
        assert "laya Router()" in str(excinfo.value)

    def test_gate_logs_the_failure_and_answers_offline(self, monkeypatch, aura_logs):
        fake_laya.install(monkeypatch)
        fake_laya.FakeRouter.raise_on_predict = "boom in the forward pass"
        gate = LayaGate(RealLayaBackend(), autoload=False)
        gate.warmup()
        # Wait for the background load, then break the model.
        for _ in range(200):
            if gate.ready:
                break
            import time

            time.sleep(0.01)
        assert gate.ready

        decision = gate.decide("open spotify", "system.open_app", {"app": "Spotify"})
        assert decision.source == "fallback"
        assert decision.backend == "heuristic"
        assert "boom in the forward pass" in decision.error
        assert any("gate call failed" in record.getMessage() and record.levelno >= logging.ERROR
                   for record in aura_logs), [r.getMessage() for r in aura_logs]
        assert gate.status()["fallbacks"] == 1
        assert gate.status()["error"]

    def test_gate_answers_immediately_while_checkpoints_load(self, monkeypatch, aura_logs):
        fake_laya.install(monkeypatch)
        gate = LayaGate(RealLayaBackend(), autoload=False)   # load never started
        decision = gate.decide("open spotify", "system.open_app", {"app": "Spotify"})
        assert decision.source == "fallback", "an unloaded model must not stall the session"
        assert decision.error                      # …and must say why it was not used

    def test_a_slow_model_hits_the_deadline(self, monkeypatch, aura_logs):
        fake_laya.install(monkeypatch)
        fake_laya.FakeRouter.predict_delay = 1.0
        gate = LayaGate(RealLayaBackend(), call_budget_seconds=0.05, autoload=False)
        gate.real.load()                       # ready, but slow
        decision = gate.decide("open spotify", "system.open_app", {"app": "Spotify"})
        assert decision.source == "fallback"
        assert "timeout" in decision.error
        assert gate.status()["timeouts"] == 1
        # …and it stays degraded instead of paying the budget on every action.
        assert gate.status()["degraded"] is True
        again = gate.decide("open spotify", "system.open_app", {"app": "Spotify"})
        assert again.source == "fallback"

    def test_choose_falls_back_and_reports_why(self, monkeypatch):
        fake_laya.install(monkeypatch)
        fake_laya.FakeRouter.raise_on_predict = "no answer"
        gate = LayaGate(RealLayaBackend(), autoload=False)
        gate.real.load()
        choice = gate.choose({"request": "open spotify"}, "pick one",
                             {"system.open_app": "open spotify", "system.mute": "mute"})
        assert choice.source == "fallback"
        assert choice.choice == "system.open_app"      # the offline scorer still answers
        assert "no answer" in choice.error


# --------------------------------------------------------------------------- #
# Selection — logged, never swallowed                                          #
# --------------------------------------------------------------------------- #


class TestBackendSelection:
    def test_missing_package_is_reported_not_hidden(self, monkeypatch, aura_logs):
        monkeypatch.setattr(laya_mod, "laya_available", lambda: False)
        backend = build_backend(Cfg())
        assert isinstance(backend, HeuristicBackend)
        assert any("not importable" in record.getMessage()
                   and record.levelno >= logging.ERROR for record in aura_logs)

    def test_laya_backend_loads_checkpoints_in_the_background(self, monkeypatch, aura_logs):
        fake_laya.install(monkeypatch)
        backend = build_backend(Cfg())
        assert isinstance(backend, LayaGate)
        assert isinstance(backend.real, RealLayaBackend)
        assert any("loading checkpoints in the background" in record.getMessage()
                   for record in aura_logs)

    def test_heuristic_backend_is_a_deliberate_choice(self, monkeypatch, aura_logs):
        class HeuristicCfg(Cfg):
            class laya(Cfg.laya):
                backend = "heuristic"

        assert isinstance(build_backend(HeuristicCfg()), HeuristicBackend)
        assert any("deliberately" in record.getMessage() for record in aura_logs)

    def test_disabled_laya_never_loads_the_package(self, monkeypatch, aura_logs):
        class DisabledCfg(Cfg):
            class laya(Cfg.laya):
                enabled = False

        assert isinstance(build_backend(DisabledCfg()), HeuristicBackend)
        assert any("disabled in config" in record.getMessage() for record in aura_logs)

    def test_availability_probe_does_not_import_torch(self):
        assert isinstance(laya_available(), bool)


# --------------------------------------------------------------------------- #
# The diagnostic command                                                       #
# --------------------------------------------------------------------------- #


class TestLayaCheck:
    def test_self_test_passes_against_a_working_model(self, monkeypatch, tmp_path, capsys):
        fake_laya.install(monkeypatch)
        fake_laya.FakeRouter.picks["quiet the house"] = "system.toggle_dnd"
        from aura.__main__ import cmd_laya_check
        from aura.config import load_config

        cfg = load_config()
        cfg.data_dir = str(tmp_path)
        assert cmd_laya_check(cfg) == 0
        out = capsys.readouterr().out
        assert "Laya self-test" in out
        assert "RESULT: Laya is working" in out
        assert "system.toggle_dnd" in out

    def test_self_test_fails_loudly_when_the_model_breaks(self, monkeypatch, tmp_path, capsys):
        fake_laya.install(monkeypatch)
        fake_laya.FakeRouter.raise_on_predict = "weights are missing"
        from aura.__main__ import cmd_laya_check
        from aura.config import load_config

        cfg = load_config()
        cfg.data_dir = str(tmp_path)
        assert cmd_laya_check(cfg) == 2
        assert "FAILED" in capsys.readouterr().out

    def test_self_test_reports_a_missing_package(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(laya_mod, "laya_available", lambda: False)
        from aura.__main__ import cmd_laya_check
        from aura.config import load_config

        cfg = load_config()
        cfg.data_dir = str(tmp_path)
        assert cmd_laya_check(cfg) == 2
        assert "NOT FOUND" in capsys.readouterr().out
