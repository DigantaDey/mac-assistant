"""Safety gate: predictable, layered, honest."""

from __future__ import annotations

from aura.laya import HeuristicBackend
from aura.planner import Action
from aura.safety import SafetyGate


def make_gate(**overrides):
    from aura.config import load_config
    cfg = load_config()
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        setattr(getattr(cfg, section), field, value)
    return SafetyGate(cfg, HeuristicBackend())


KNOWN = {"system.open_app", "system.empty_trash", "system.sleep", "system.set_volume",
         "shell.run"}


class TestBlocklist:
    def test_blocked_pattern_refuses(self):
        gate = make_gate()
        action = Action("system.open_app", {"app": "Terminal"})
        v = gate.assess(action, "run sudo dd if=/dev/zero", KNOWN)
        assert v.decision == "blocked"


class TestUnknownSkill:
    def test_unknown_skill_blocked(self):
        gate = make_gate()
        action = Action("chatgpt.skynet", {})
        assert gate.assess(action, "do the thing", KNOWN).decision == "blocked"


class TestLayaGate:
    def test_destructive_requires_confirmation(self):
        gate = make_gate()
        v = gate.assess(Action("system.empty_trash", {}), "empty the trash", KNOWN)
        assert v.decision == "confirm"
        assert any("destructive" in r for r in v.reasons)

    def test_safe_action_runs(self):
        gate = make_gate()
        v = gate.assess(Action("system.open_app", {"app": "Spotify"}),
                        "open spotify", KNOWN)
        assert v.decision == "run"
        assert "laya[heuristic]" in v.reasons[0]

    def test_destructive_args_bump_score(self):
        gate = make_gate()
        v = gate.assess(Action("system.open_app", {"app": "rm -rf ~/Library"}),
                        "open spotify", KNOWN)
        assert v.decision == "confirm"

    def test_low_match_confidence_asks(self):
        gate = make_gate()
        # args completely unrelated to the transcript → match below threshold
        v = gate.assess(Action("system.set_volume", {"level": 3}),
                        "what is the weather on mars", KNOWN)
        assert v.decision == "confirm"
        assert any("match" in r for r in v.reasons)

    def test_confirm_disabled_runs_anyway_when_safe(self):
        gate = make_gate(safety__confirm_destructive=False)
        v = gate.assess(Action("system.empty_trash", {}), "empty the trash", KNOWN)
        assert v.decision == "run"


class TestWordsOverride:
    """A small decision model can miscalibrate an everyday request (match 0.15
    for 'open youtube.com in safari' happened in the wild). For SAFE actions
    whose words demonstrably match, the deterministic word check overrides the
    low model score — asking there is friction, not caution. Nothing risky is
    ever overridden."""

    @staticmethod
    def _laya(match: float, destructive: float):
        from aura.laya import Decision

        return Decision(match=match, destructive=destructive,
                        backend="laya", source="real", ms=42.0)

    KNOWN = frozenset({"browser.open_url", "system.set_volume", "system.empty_trash"})

    def test_a_safe_word_matched_action_overrides_a_low_model_match(self):
        gate = make_gate()
        v = gate.assess(
            Action("browser.open_url", {"url": "youtube.com", "browser": "Safari"}, "safe"),
            "open youtube.com in safari", self.KNOWN,
            decision=self._laya(0.15, 0.0))
        assert v.decision == "run"
        assert any("offline check" in r for r in v.reasons)

    def test_url_arguments_match_their_domain_in_the_request(self):
        gate = make_gate()
        v = gate.assess(
            Action("browser.open_url", {"url": "https://www.youtube.com"}, "safe"),
            "open youtube.com", self.KNOWN,
            decision=self._laya(0.30, 0.0))
        assert v.decision == "run"

    def test_a_risky_action_is_never_overridden(self):
        gate = make_gate()
        v = gate.assess(Action("system.empty_trash", {}, "confirm"),
                        "empty the trash", self.KNOWN,
                        decision=self._laya(0.20, 0.90))
        assert v.decision == "confirm"

    def test_a_model_destructive_flag_is_never_overridden(self):
        gate = make_gate()
        v = gate.assess(
            Action("browser.open_url", {"url": "youtube.com"}, "safe"),
            "open youtube.com", self.KNOWN,
            decision=self._laya(0.10, 0.80))
        assert v.decision == "confirm"

    def test_an_unrelated_safe_action_still_asks(self):
        gate = make_gate()
        v = gate.assess(Action("system.set_volume", {"level": 3}, "safe"),
                        "what is the weather on mars", self.KNOWN,
                        decision=self._laya(0.10, 0.0))
        assert v.decision == "confirm"
        assert any("match" in r for r in v.reasons)

    def test_the_offline_scorer_is_not_overridden_by_itself(self):
        gate = make_gate()
        from aura.laya import Decision

        heuristic = Decision(match=0.10, destructive=0.0,
                             backend="heuristic", source="heuristic", ms=0.1)
        v = gate.assess(Action("system.set_volume", {"level": 3}, "safe"),
                        "what is the weather on mars", self.KNOWN,
                        decision=heuristic)
        assert v.decision == "confirm"
