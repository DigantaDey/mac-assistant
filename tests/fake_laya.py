"""A stand-in for the published `laya` package.

The adapter in `aura/laya.py` is only correct if it matches the API that
`pip install laya` actually ships:

    from laya import Router
    router = Router(preload=True)
    result = router.predict(state, {
        "match": {"type": "noul", "instructions": "…"},
        "pick":  {"type": "choice", "instructions": "…", "criteria": {label: text}},
        "level": {"type": "score",  "instructions": "…", "criteria": ["0", "10", …]},
    })
    result["answers"]["match"]["noul"]        # probability the answer is yes
    result["answers"]["pick"]["choice"]       # the chosen label
    result["answers"]["pick"]["probabilities"]  # every label's probability
    result["answers"]["level"]["score"]       # expected index on the scale
    result["routing"]["model"]                # which checkpoint answered
    router.predict_batch([{"state": state, "questions": questions}, ...]) → results
    router.loaded()                           # names of resident checkpoints

Tests install this in `sys.modules` so the real code path runs — no torch, no
downloads, no flakiness — and every call is recorded for assertions.
"""

from __future__ import annotations

import importlib.machinery
import json
import re
import sys
import types
from typing import Any, ClassVar

VERSION = "0.3.21-test"

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text).lower()) if len(w) > 2}


def _state_request(state: Any) -> str:
    if isinstance(state, str):
        try:
            return str(json.loads(state).get("request", ""))
        except json.JSONDecodeError:
            return state
    if isinstance(state, dict):
        if "request" in state:
            return str(state["request"])
        return " ".join(str(v) for v in state.values()
                        if not isinstance(v, (dict, list)))
    return str(state)


def _state_action(state: Any) -> dict[str, Any]:
    if isinstance(state, str):
        try:
            return dict(json.loads(state).get("action") or {})
        except json.JSONDecodeError:
            return {}
    return {}


class FakeRouter:
    """A deterministic `laya.Router` that answers the same shapes."""

    # -- test knobs (class attributes: set them in a test, reset after) ------ #
    picks: ClassVar[dict[str, str]] = {}  # request text → chosen label
    noul: ClassVar[dict[str, float]] = {}  # question id → probability of "yes"
    raise_on_predict: str = ""            # raise this instead of answering
    predict_delay: float = 0.0            # simulate a slow forward pass
    fail_load: str = ""                   # Router(...) raises
    instances: ClassVar[list[FakeRouter]] = []

    def __init__(self, preload: bool = False, max_loaded: int = 2, device: str | None = None,
                 default: str | None = None, models: dict | None = None,
                 **kwargs: Any) -> None:
        if FakeRouter.fail_load:
            raise RuntimeError(FakeRouter.fail_load)
        self.preload = preload
        self.max_loaded = max_loaded
        self.device = device
        self.default = default
        self.models = models or {}
        self.kwargs = kwargs
        self.calls: list[tuple[Any, dict]] = []
        self.batches: list[list[dict[str, Any]]] = []
        FakeRouter.instances.append(self)

    # -- the API ------------------------------------------------------------ #

    def predict(self, state: Any, questions: dict, model: str | None = None,
                **kwargs: Any) -> dict:
        import time

        if FakeRouter.predict_delay:
            time.sleep(FakeRouter.predict_delay)
        if FakeRouter.raise_on_predict:
            raise RuntimeError(FakeRouter.raise_on_predict)
        self.calls.append((state, questions))
        return self._answer(state, questions)

    def predict_batch(self, requests: list[dict[str, Any]], **kwargs: Any) -> list[dict]:
        """Match Laya 0.3's heterogeneous request-batch API."""
        if FakeRouter.raise_on_predict:
            raise RuntimeError(FakeRouter.raise_on_predict)
        self.batches.append(list(requests))
        return [self._answer(request["state"], request["questions"])
                for request in requests]

    def loaded(self) -> list[str]:
        return ["english"]

    def unload(self, name: str | None = None) -> None:
        return None

    # -- answers ------------------------------------------------------------ #

    def _answer(self, state: Any, questions: dict) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for qid, question in questions.items():
            qtype = question.get("type")
            if qtype == "noul":
                p = self._noul(state, qid, question)
                answers[qid] = {"type": "noul", "noul": round(p, 4),
                                "confidence": round(max(p, 1 - p), 4),
                                "action": {"act_probability": 0.9}}
            elif qtype == "choice":
                criteria = list(question.get("criteria") or {})
                probs = self._choice_probs(state, criteria)
                best = max(probs, key=lambda k: probs[k]) if probs else ""
                answers[qid] = {"type": "choice", "choice": best,
                                "probabilities": {k: round(v, 4) for k, v in probs.items()},
                                "confidence": round(probs.get(best, 0.0), 4),
                                "action": {"act_probability": 0.9}}
            elif qtype == "score":
                criteria = list(question.get("criteria") or [])
                index = self._score_index(state, criteria)
                answers[qid] = {"type": "score", "score": round(index, 4),
                                "legend": {str(i): c for i, c in enumerate(criteria)},
                                "probabilities": {str(i): 1.0 / max(1, len(criteria))
                                                  for i in range(len(criteria))},
                                "confidence": 0.8, "action": {"act_probability": 0.9}}
        return {"answers": answers,
                "routing": {"model": "english", "repo": "convaiinnovations/laya/english",
                            "reason": "Latin script, language not identified; using default (english)"},
                "usage": {"states": 1, "questions": len(questions), "tokens": 42},
                "elapsed_ms": 33.0}

    def _noul(self, state: Any, qid: str, question: dict) -> float:
        if qid in FakeRouter.noul:
            return FakeRouter.noul[qid]
        request = _state_request(state).lower()
        action = _state_action(state)
        skill = str(action.get("skill", ""))
        args = action.get("args") or {}
        if qid == "destructive":
            if any(word in skill for word in ("empty_trash", "quit_app", "sleep")):
                return 0.95
            if any(word in json.dumps(args).lower() for word in ("delete", "send", "submit")):
                return 0.9
            return 0.05
        # "match": does the action's own content appear in the request?
        words = _tokens(" ".join(str(v) for v in args.values()) + " " + skill.replace(".", " "))
        if not words:
            return 0.8
        hits = sum(1 for w in words if w in request)
        return 0.9 if hits else 0.15

    def _choice_probs(self, state: Any, criteria: list[str]) -> dict[str, float]:
        request = _state_request(state).lower()
        if request.strip() in FakeRouter.picks:
            chosen = FakeRouter.picks[request.strip()]
            return {label: (0.91 if label == chosen else 0.09 / max(1, len(criteria) - 1))
                    for label in criteria}
        request_words = _tokens(request)
        scores = {}
        for label in criteria:
            words = _tokens(label)
            scores[label] = (len(request_words & words) / len(request_words)) if request_words else 0.0
        total = sum(scores.values()) or 1.0
        return {label: score / total for label, score in scores.items()}

    def _score_index(self, state: Any, criteria: list[str]) -> float:
        request = _state_request(state).lower()
        digits = re.search(r"\b(\d{1,3})\b", request)
        if digits:
            value = max(0, min(100, int(digits.group(1))))
            return value / 10.0
        return 5.0


def install(monkeypatch) -> types.ModuleType:
    """Put a fake `laya` package on `sys.modules` for the duration of a test."""
    module = types.ModuleType("laya")
    module.__version__ = VERSION
    module.Router = FakeRouter
    module.__spec__ = importlib.machinery.ModuleSpec("laya", loader=None)
    module.__all__ = ["Router"]
    FakeRouter.instances = []
    FakeRouter.picks = {}
    FakeRouter.noul = {}
    FakeRouter.raise_on_predict = ""
    FakeRouter.predict_delay = 0.0
    FakeRouter.fail_load = ""
    monkeypatch.setitem(sys.modules, "laya", module)
    return module
