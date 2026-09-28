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
