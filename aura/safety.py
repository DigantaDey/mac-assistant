"""Safety — the part that must never be clever, only predictable.

Three independent layers, checked in order:

1. Blocklist       — raw substrings that are refused outright, no matter how
                     the planner phrased them.
2. Skill manifest  — every skill declares a default risk; unknown skills
                     default to "confirm".
3. Laya gate       — calibrated destructive/match scores; below-threshold
                     confidence is treated as *risk*, never as permission.

The output is a simple verdict the orchestrator can act on:
    run            — execute now
    confirm        — show the plan, wait for an explicit yes
    blocked        — refuse with an explanation
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Verdict:
    decision: str                       # run | confirm | blocked
    reasons: list[str] = field(default_factory=list)

    @property
    def ok_to_run(self) -> bool:
        return self.decision == "run"


class SafetyGate:
    def __init__(self, cfg, laya_backend) -> None:
        self.cfg = cfg
        self.laya = laya_backend

    def assess(self, action, transcript: str, known_skills: set[str]) -> Verdict:
        reasons: list[str] = []

        # 1 — absolute blocklist (substring over the raw transcript + args blob)
        blob = transcript + " " + str(action.args)
        for pattern in self.cfg.safety.blocked_patterns:
            if pattern.lower() in blob.lower():
                return Verdict("blocked", [f"matches blocklist: {pattern!r}"])

        # 2 — skill knowledge
        if action.skill not in known_skills:
            return Verdict("blocked", [f"unknown skill {action.skill!r}"])

        # 3 — Laya gate
        decision = self.laya.decide(transcript, action.skill, action.args, action.why)
        if decision.destructive >= self.cfg.laya.destructive_threshold:
            reasons.append(f"destructive score {decision.destructive:.2f}")
        if decision.match < self.cfg.laya.confidence_threshold:
            reasons.append(f"match confidence {decision.match:.2f} below threshold")

        confirmed_by_default = action.risk == "safe"
        if self.cfg.safety.confirm_destructive and reasons:
            return Verdict("confirm", reasons)
        if not confirmed_by_default:
            reasons.append("skill requests confirmation")
            return Verdict("confirm", reasons)
        return Verdict("run", [f"laya[{decision.backend}] "
                               f"match={decision.match:.2f} destructive={decision.destructive:.2f}"])
