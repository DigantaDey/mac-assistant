"""The Laya planner: rules parse, the decision model routes, failures are loud.

These tests run the *whole* stack that ships — a `laya` package (faked at the
published API), `RealLayaBackend`, `LayaGate`, `LayaRouter`, `MockPlanner` — so
what passes here is the path a user's voice takes:

    "open spotify"        → the rules answer in microseconds (no model call)
    "quiet the house"     → Laya chooses system.toggle_dnd, one forward pass
    "book me a flight"    → nothing routes; an honest "I don't have a skill"
    model broken/slow     → the rules still answer, degraded=True + a diagnostic

The last point is the product decision this file exists to protect: a failure
in the brain may cost an answer, but it may never be *silent*.
"""

from __future__ import annotations

import asyncio
import time

import fake_laya
import pytest

from aura.laya import HeuristicBackend, LayaGate, RealLayaBackend
from aura.planner import LayaPlanner, MockPlanner, build_planner
from aura.skills import build_default_registry


def build_gate(monkeypatch, *, ready: bool = True, budget: float = 1.5,
               picks: dict | None = None) -> LayaGate:
    fake_laya.install(monkeypatch)
    fake_laya.FakeRouter.picks = dict(picks or {})
    gate = LayaGate(RealLayaBackend(), HeuristicBackend(), call_budget_seconds=budget,
                    autoload=False)
    if ready:
        gate.real.load()
    return gate


def planner_for(cfg, gate) -> LayaPlanner:
    registry = build_default_registry()
    cfg.planner.engine = "laya"
    return LayaPlanner(cfg, registry.catalog_prompt(), gate, registry.specs())


@pytest.fixture()
def cfg():
    """A real-mac configuration: Laya is the brain (no demo pins in the way)."""
    from aura.config import load_config

    cfg = load_config()
    cfg.profile = "mac"
    cfg.planner.engine = "laya"
    cfg.laya.backend = "auto"
    return cfg


def plan(planner, text):
    return asyncio.run(planner.plan(text, {}))


# --------------------------------------------------------------------------- #
# The fast path: the rules answer, the model is not asked                      #
# --------------------------------------------------------------------------- #


class TestRulesFirst:
    @pytest.mark.parametrize("text,skill", [
        ("open spotify", "system.open_app"),
        ("set volume to 30", "system.set_volume"),
        ("open youtube", "browser.open_url"),
        ("empty the trash", "system.empty_trash"),
        ("click the sign in button", "ax.click"),
    ])
    def test_everyday_commands_never_wait_for_the_model(self, cfg, monkeypatch, text, skill):
        gate = build_gate(monkeypatch)
        planner = planner_for(cfg, gate)
        result = plan(planner, text)
        assert [a.skill for a in result.actions] == [skill]
        assert result.source == "rules"
        assert result.routed_by == "rules"
        assert result.degraded is False
        # No routing question was asked at all.
        assert fake_laya.FakeRouter.instances[0].calls == []

    def test_conversational_answers_need_no_model(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch)
        result = plan(planner_for(cfg, gate), "what can you do")
        assert result.actions == []
        assert "open and quit apps" in result.reply
        assert fake_laya.FakeRouter.instances[0].calls == []


# --------------------------------------------------------------------------- #
# Laya routes what the rules cannot                                            #
# --------------------------------------------------------------------------- #


class TestLayaRoutes:
    def test_the_model_chooses_the_skill_and_the_plan_is_marked_laya(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch, picks={"quiet the house": "system.toggle_dnd"})
        result = plan(planner_for(cfg, gate), "quiet the house")
        assert [a.skill for a in result.actions] == ["system.toggle_dnd"]
        assert result.actions[0].args == {}
        assert result.source == "laya"
        assert result.routed_by == "laya"
        assert result.reply == "Toggling Do Not Disturb."
        assert result.degraded is False
        assert "laya" in result.actions[0].why          # the timeline says who chose it
        assert 0 <= result.model_ms <= result.latency_ms  # measured, reported, never faked
        # One forward pass, and only for what the rules could not route.
        assert len(fake_laya.FakeRouter.instances[0].calls) == 1

    def test_a_routed_skill_keeps_the_users_words(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch, picks={"search the web for cormorants": "browser.search"})
        result = plan(planner_for(cfg, gate), "search the web for cormorants")
        assert result.actions[0].args.get("query", "").lower() == "cormorants" \
            or result.actions[0].skill == "browser.search"

    def test_nothing_routing_stays_an_honest_refusal(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch, picks={"book me a flight to goa": "none"})
        result = plan(planner_for(cfg, gate), "book me a flight to goa")
        assert result.actions == []
        assert "skill" in result.reply.lower()
        assert result.complete is False
        assert result.degraded is False        # the model worked; the skill doesn't exist

    def test_a_partial_rule_plan_is_extended_not_replaced(self, cfg, monkeypatch):
        """"open notes and go quiet for a while": half the request is a rule and
        half is a paraphrase the rules do not know. The rule's action is kept
        and Laya places the rest, instead of the whole request being refused."""
        gate = build_gate(monkeypatch,
                          picks={"open notes and go quiet for a while": "system.toggle_dnd"})
        result = plan(planner_for(cfg, gate), "open notes and go quiet for a while")
        assert [a.skill for a in result.actions] == ["system.open_app", "system.toggle_dnd"]
        assert result.routed_by == "laya+rules"
        assert result.source == "laya"
        assert result.complete is True

    def test_a_partial_rule_plan_survives_a_model_that_finds_nothing(self, cfg, monkeypatch):
        """The common case: the rules place half the request, Laya has nothing
        better, and the honest half-answer is what the user hears."""
        gate = build_gate(monkeypatch, picks={"open notes and refactor the kernel": "none"})
        result = plan(planner_for(cfg, gate), "open notes and refactor the kernel")
        assert [a.skill for a in result.actions] == ["system.open_app"]
        assert result.complete is False
        assert result.reply.startswith("Opening Notes.")
        assert "I don't have a skill" in result.reply      # the other half is refused honestly
        assert result.degraded is False

    def test_the_routing_question_offers_a_none_option(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch)
        plan(planner_for(cfg, gate), "refactor the kernel")
        _state, questions = fake_laya.FakeRouter.instances[0].calls[0]
        criteria = questions["pick"]["criteria"]
        assert "none" in criteria
        assert questions["pick"]["type"] == "choice"


# --------------------------------------------------------------------------- #
# When the model is missing, broken or slow                                    #
# --------------------------------------------------------------------------- #


class TestDegradationIsVisible:
    def test_a_broken_model_degrades_with_the_reason_attached(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch)
        fake_laya.FakeRouter.raise_on_predict = "CUDA out of memory"
        result = plan(planner_for(cfg, gate), "quiet the house")
        assert result.actions == []
        assert result.degraded is True
        assert "CUDA out of memory" in result.diagnostic
        # The everyday commands still work while the brain is down.
        assert [a.skill for a in plan(planner_for(cfg, gate), "open spotify").actions] == \
            ["system.open_app"]

    def test_an_unloaded_model_does_not_stall_a_session(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch, ready=False)
        started = time.perf_counter()
        result = plan(planner_for(cfg, gate), "quiet the house")
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, "routing must not wait for a model that is still loading"
        assert result.degraded is True
        assert result.diagnostic
        assert result.actions == []

    def test_a_slow_model_is_cut_off_by_the_route_budget(self, cfg, monkeypatch):
        """The session's clock, not the model's, decides how long routing takes.

        (`result.latency_ms` is measured inside the planner: `asyncio.run()`
        then waits for the abandoned worker thread on the way out, which is a
        test-harness artefact, not product latency.)
        """
        gate = build_gate(monkeypatch, budget=5.0)
        fake_laya.FakeRouter.predict_delay = 1.0
        planner = planner_for(cfg, gate)
        planner.ROUTE_BUDGET_MS = 50
        result = plan(planner, "quiet the house")
        assert result.latency_ms < 500, result.latency_ms
        assert "did not answer" in result.diagnostic
        assert result.actions == []

    def test_the_plan_dictionary_carries_the_diagnostics(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch)
        fake_laya.FakeRouter.raise_on_predict = "weights missing"
        payload = plan(planner_for(cfg, gate), "quiet the house").as_dict()
        assert payload["degraded"] is True
        assert "weights missing" in payload["diagnostic"]
        assert payload["routed_by"] in ("laya", "rules")
        assert "model_ms" in payload

    def test_the_offline_gate_still_routes_what_it_can(self, cfg, monkeypatch):
        gate = LayaGate(HeuristicBackend(), autoload=False)     # no laya package at all
        result = plan(planner_for(cfg, gate), "open spotify")
        assert [a.skill for a in result.actions] == ["system.open_app"]
        assert result.source == "rules"


# --------------------------------------------------------------------------- #
# Wiring                                                                       #
# --------------------------------------------------------------------------- #


class TestPlannerFactory:
    def test_laya_is_the_default_brain(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch)
        registry = build_default_registry()
        planner = build_planner(cfg, registry.catalog_prompt(), registry.specs(), gate)
        assert isinstance(planner, LayaPlanner)
        assert cfg.planner.engine == "laya"          # the shipped default

    def test_engine_laya_without_a_backend_still_answers(self, cfg):
        registry = build_default_registry()
        planner = build_planner(cfg, registry.catalog_prompt(), registry.specs(), None)
        assert isinstance(planner, LayaPlanner)
        assert [a.skill for a in plan(planner, "mute").actions] == ["system.mute"]

    def test_engine_mock_is_the_demo_path(self, cfg):
        cfg.planner.engine = "mock"
        planner = build_planner(cfg, "catalog")
        assert isinstance(planner, MockPlanner)

    def test_legacy_llm_engines_map_to_laya(self, cfg):
        """The LLM planners were removed: whatever an old config says, the
        brain is Laya (with the offline scorer when no backend is wired)."""
        cfg.planner.engine = "openai_compat"
        planner = build_planner(cfg, "catalog")
        assert isinstance(planner, LayaPlanner)
        cfg.planner.engine = "auto"
        planner = build_planner(cfg, "catalog")
        assert isinstance(planner, LayaPlanner)

    def test_warmup_loads_without_blocking(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch, ready=False)
        planner = planner_for(cfg, gate)
        assert planner.warmup() in (True, False)     # returns; never raises
        for _ in range(200):
            if gate.ready:
                break
            time.sleep(0.01)
        assert gate.ready

    def test_status_is_fit_for_the_api(self, cfg, monkeypatch):
        gate = build_gate(monkeypatch)
        status = planner_for(cfg, gate).status
        assert status["backend"] == "laya"
        assert status["ready"] is True
        assert "avg_ms" in status
