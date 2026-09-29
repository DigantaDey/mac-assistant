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

The gate is *called* with a decision when the orchestrator already asked Laya
(one forward pass covers every action of a request — see
`aura.laya.LayaGate.decide_many`), and asks for one itself otherwise. Either
way it cannot raise: a failure inside the decision layer is logged with its
traceback and the offline scorer answers, because an exception here would mean
an action nobody judged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .laya import Decision, HeuristicBackend
from .log import get_logger, log_exception

log = get_logger("safety")


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

    def assess(self, action, transcript: str, known_skills: set[str],
               decision: Decision | None = None) -> Verdict:
        reasons: list[str] = []

        # 1 — absolute blocklist (substring over the raw transcript + args blob)
        blob = transcript + " " + str(action.args)
        for pattern in self.cfg.safety.blocked_patterns:
            if pattern.lower() in blob.lower():
                log.warning("gate: blocked %s — matches blocklist %r",
                            action.skill, pattern)
                return Verdict("blocked", [f"matches blocklist: {pattern!r}"])

        # 2 — skill knowledge
        if action.skill not in known_skills:
            log.warning("gate: blocked unknown skill %r", action.skill)
            return Verdict("blocked", [f"unknown skill {action.skill!r}"])

        # 3 — Laya gate
        decision = decision or self._decide(transcript, action)
        if decision.destructive >= self.cfg.laya.destructive_threshold:
            reasons.append(f"destructive score {decision.destructive:.2f}")
        if decision.match < self.cfg.laya.confidence_threshold:
            reasons.append(f"match confidence {decision.match:.2f} below threshold")

        confirmed_by_default = action.risk == "safe"
        if self.cfg.safety.confirm_destructive and reasons:
            verdict = Verdict("confirm", reasons)
        elif not confirmed_by_default:
            reasons.append("skill requests confirmation")
            verdict = Verdict("confirm", reasons)
        else:
            verdict = Verdict("run", [f"laya[{decision.backend}] "
                                      f"match={decision.match:.2f} "
                                      f"destructive={decision.destructive:.2f}"])

        log.info("gate: %s → %s (match=%.2f destructive=%.2f via %s%s, %.1fms)",
                 action.skill, verdict.decision, decision.match, decision.destructive,
                 decision.backend, f" {decision.error}" if decision.error else "",
                 decision.ms)
        return verdict

    def _decide(self, transcript: str, action) -> Decision:
        """Ask the decision layer for this action — or answer offline, loudly."""
        try:
            return self.laya.decide(transcript, action.skill, action.args, action.why)
        except Exception as exc:
            detail = log_exception(
                f"safety: the Laya gate raised while judging {action.skill} — "
                "the offline scorer answered instead", exc, logger=log)
            fallback = HeuristicBackend().decide(transcript, action.skill, action.args,
                                                 action.why)
            fallback.error = detail
            fallback.source = "fallback"
            return fallback
