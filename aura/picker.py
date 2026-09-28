"""Element picking — which UI element did the user mean?

The naive approach (score every element against the query) is fine for a
dozen elements and degrades past ~20 options — exactly where Laya's accuracy
drops (0.425 on 77-way choices). So this picker follows the coarse-to-fine
recipe the laya-browser fine-tune validated:

    1. flatten the AX tree into labeled options ("button 'Sign In'")
    2. COARSE: score *chunks* of ≤16 options ("does the answer live here?")
    3. FINE: score individual options only inside the top chunks
    4. threshold → best pick, or honest "did you mean …" suggestions

With the real Laya backend each score is one ~4-33 ms forward pass with a
calibrated probability — never free-form text, so the picker cannot
hallucinate an element that isn't on screen. The heuristic scorer (demo/CI)
is deterministic token overlap with a role prior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ax import AXNode

_CHUNK = 16
_STOP = {"the", "a", "an", "please", "my", "on", "in", "to"}

# Words that describe *what kind* of control the user means, not what it's
# called — kept for the role prior, excluded from content matching so
# "sign in button" doesn't dilute to {sign}.
_ROLE_WORDS = {"button", "link", "field", "tab", "menu", "checkbox", "control",
               "item", "icon", "bar", "section", "option"}

# Query words that hint at a role, used as a small, honest prior.
_ROLE_HINTS = {
    "button": {"button", "press", "click", "tap", "hit"},
    "link": {"link", "open", "go"},
    "textfield": {"field", "type", "enter", "fill", "write"},
    "searchfield": {"search", "field", "type"},
    "checkbox": {"checkbox", "check", "tick", "toggle"},
    "menu": {"menu", "item"},
}


def option_text(node: AXNode) -> str:
    return node.label or node.value or ""


def score_query_tokens(query: str) -> set[str]:
    return {t for t in re.split(r"\W+", query.lower()) if t and t not in _STOP}


class HeuristicScorer:
    """Deterministic token overlap with a role prior. Explainable, testable."""

    def score_option(self, query: str, node: AXNode) -> float:
        text = option_text(node).lower()
        if not text:
            return 0.0
        q_all = score_query_tokens(query)
        if not q_all:
            return 0.0
        q_content = q_all - _ROLE_WORDS or q_all
        n_tokens = set(re.split(r"\W+", text.lower()))

        overlap = len(q_content & n_tokens) / len(q_content)
        score = overlap * 0.75
        # Contiguous phrase match, scaled by how much of the label it covers —
        # "sign" inside "sign in" beats "sign" inside "sign up for github".
        phrase = " ".join(q_content)
        if phrase and phrase in text:
            score = max(score, 0.97 if phrase == text.strip()
                        else 0.95 - 0.02 * max(0, len(n_tokens) - len(q_content)))
        elif len(q_content) > 1 and q_content <= n_tokens:
            score = max(score, 0.85)   # all words present, order doesn't
        # role prior: a *tiebreaker* (+0.04) — the label decides, the role nudges
        for role, hints in _ROLE_HINTS.items():
            if node.role == role and q_all & hints:
                score = min(0.99, score + 0.04)
        # precision bonus: label barely longer than the query → likely exact
        if len(n_tokens) <= len(q_content) + 1:
            score = min(0.99, score + 0.02)
        return min(score, 0.99)

    def score_chunk(self, query: str, nodes: list[AXNode]) -> float:
        # Honest for a deterministic scorer: the best option in the chunk.
        return max((self.score_option(query, n) for n in nodes), default=0.0)


class LayaScorer:
    """The real thing: typed questions to the Laya decision model.

    Coarse pass asks one yes/no question per chunk; the fine pass scores the
    individual options of the winning chunks. Every answer is a calibrated
    probability over a fixed option set — there is no free text, so nothing
    here can hallucinate.
    """

    def __init__(self, backend) -> None:  # noqa: ANN001 - aura.laya.LayaBackend-like
        self.backend = backend

    def score_option(self, query: str, node: AXNode) -> float:
        decision = self.backend.decide(
            query, "ax.pick_element",
            {"element": option_text(node), "role": node.role}, "")
        return decision.match

    def score_chunk(self, query: str, nodes: list[AXNode]) -> float:
        labels = [option_text(n) for n in nodes[:_CHUNK]]
        decision = self.backend.decide(
            query, "ax.pick_element",
            {"candidates": labels}, "")
        return decision.match


@dataclass
class PickResult:
    best: AXNode | None = None
    best_label: str = ""
    confidence: float = 0.0
    suggestions: list[tuple[str, float]] = field(default_factory=list)
    considered: int = 0
    chunks: int = 0


class ElementPicker:
    def __init__(self, scorer=None, threshold: float = 0.62,
                 chunk_size: int = _CHUNK, top_chunks: int = 2) -> None:
        self.scorer = scorer or HeuristicScorer()
        self.threshold = threshold
        self.chunk_size = chunk_size
        self.top_chunks = top_chunks

    def pick(self, query: str, root: AXNode,
             want_roles: tuple[str, ...] | None = None) -> PickResult:
        options = [
            n for n in root.flat()
            if option_text(n)
            and n is not root
            and (want_roles is None or n.role in want_roles)
        ]
        result = PickResult(considered=len(options))
        if not options:
            return result

        # 1 — chunk
        chunks = [options[i:i + self.chunk_size]
                  for i in range(0, len(options), self.chunk_size)]
        result.chunks = len(chunks)

        # 2 — coarse ranking over chunks
        ranked = sorted(
            ((self.scorer.score_chunk(query, chunk), chunk) for chunk in chunks),
            key=lambda pair: pair[0], reverse=True,
        )

        # 3 — fine scoring inside the top chunks only
        candidates: list[tuple[float, AXNode]] = []
        for _, chunk in ranked[: self.top_chunks]:
            for node in chunk:
                candidates.append((self.scorer.score_option(query, node), node))
        candidates.sort(key=lambda pair: pair[0], reverse=True)

        top = candidates[:3]
        result.suggestions = [(option_text(n), s) for s, n in top if s > 0.05]
        if top and top[0][0] >= self.threshold:
            result.best = top[0][1]
            result.best_label = option_text(top[0][1])
            result.confidence = round(top[0][0], 3)
        elif top:
            result.confidence = round(top[0][0], 3)
        return result


def default_picker(laya_backend=None, threshold: float = 0.62) -> ElementPicker:
    """Heuristic by default; real Laya as soon as the backend supports it."""
    if laya_backend is not None and type(laya_backend).__name__ != "HeuristicBackend":
        return ElementPicker(LayaScorer(laya_backend), threshold=threshold)
    return ElementPicker(HeuristicScorer(), threshold=threshold)
