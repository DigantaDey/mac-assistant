"""The Laya layer — the fast, calibrated decision engine that runs Aura.

Laya (Apache-2.0, Convai Innovations) is a non-autoregressive "System 1"
model: it answers *typed questions* about a state with calibrated
probabilities in a single forward pass. It never generates text, so it cannot
hallucinate an instruction, and because every question is answered in the same
pass it is fast enough to sit on the critical path.

Aura uses exactly three question types, from the `laya` package's own API:

    choice   pick one label from a closed set   → "which skill is this?"
    score    place the state on an ordered scale → "how loud should it be?"
    noul     yes/no probability                  → "is this destructive?"

This module owns:

  * `RealLayaBackend` — our adapter over `laya.Router`. One file to fix if the
    upstream API moves, and a self-test (`python -m aura laya-check`) that
    proves the adapter against whatever version is installed.
  * `HeuristicBackend` — the same three answers from explicit, deterministic
    rules. It is what the demo profile, CI and a machine without the `laya`
    package run on, so the product never depends on a 1 GB download to work.
  * `LayaGate` — the object the rest of Aura talks to: it owns the real
    backend, loads it in the background, bounds every call with a deadline,
    and falls back to the heuristic answers *with the reason recorded* instead
    of pretending nothing happened.
  * `ExampleBuffer` — the supervision set: every gated decision with the
    outcome the user chose, exported as the JSONL a Laya fine-tune consumes.

Honesty rules that shaped the code below:

  * A missing or broken Laya is loud, not silent: the reason and the full
    traceback are logged, `status()` reports it, and every `Decision` that
    came from the fallback carries `error`.
  * The gate's questions are a stable contract (`GATE_QUESTIONS`): the
    fine-tune dataset, the heuristics and the real model all answer the same
    two questions in the same words.
  * No layer below this one may raise into a session. The gate returns an
    answer, or an answer plus an explanation — never an exception.
"""

from __future__ import annotations

import concurrent.futures as _futures
import importlib.util
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .log import describe_exception, get_logger, traceback_text

log = get_logger("laya")

# --------------------------------------------------------------------------- #
# The gate's questions — one stable contract for model, heuristics and data    #
# --------------------------------------------------------------------------- #

# The wording is a contract, not a comment: it is what the gate asks at
# runtime, what the heuristics are written against, and what the fine-tune
# dataset (ExampleBuffer.export_jsonl) already contains. Change it and every
# previously exported example becomes a different question.
Q_MATCH = "Does this action match what the user asked for?"
Q_DESTRUCTIVE = "Is this action destructive, irreversible, or shared beyond the device?"

GATE_QUESTIONS: dict[str, dict[str, str]] = {
    "match": {"type": "noul", "instructions": Q_MATCH},
    "destructive": {"type": "noul", "instructions": Q_DESTRUCTIVE},
}

#: How long a single Laya call may take before the offline gate answers
#: instead. A warm Laya answers in tens of milliseconds; this budget exists
#: for cold checkpoint loads and slow CPUs. Laya is not an LLM — the budget
#: is a freeze detector with honest headroom, not a performance target — and
#: it keeps the gate inside the session's active-request budget.
DEFAULT_CALL_BUDGET_SECONDS = 6.0
#: After a timeout, stop asking for this long: a stuck model must not add its
#: budget to every action of every session.
DEGRADE_COOLDOWN_SECONDS = 120.0


class LayaError(RuntimeError):
    """A Laya failure with enough context for an engineer to act on it."""

    def __init__(self, stage: str, exc: BaseException | str) -> None:
        self.stage = stage
        self.original = exc
        self.detail = describe_exception(exc) if isinstance(exc, BaseException) else str(exc)
        super().__init__(f"laya {stage}: {self.detail}")


@dataclass
class Decision:
    """The gate's verdict inputs: two calibrated probabilities."""

    match: float                    # P(action matches the request)
    destructive: float              # P(destructive / irreversible / off-device)
    backend: str = "heuristic"      # "laya" | "heuristic"
    ms: float = 0.0
    model: str = ""                 # checkpoint the router chose ("english", …)
    routing: str = ""               # why it chose it
    error: str = ""                 # set when the real backend failed and this is a fallback
    source: str = ""                # "real" | "fallback" — which one answered

    @property
    def safe_confidence(self) -> float:
        return self.match * (1.0 - self.destructive)

    @property
    def real(self) -> bool:
        return self.source == "real"

    def as_dict(self) -> dict[str, Any]:
        return {
            "match": round(self.match, 4),
            "destructive": round(self.destructive, 4),
            "backend": self.backend,
            "source": self.source or ("real" if self.backend == "laya" else "heuristic"),
            "ms": round(self.ms, 1),
            "model": self.model,
            "routing": self.routing,
            "error": self.error,
        }


@dataclass
class Choice:
    """The answer to a `choice` question: one label, with its calibrated probability."""

    choice: str = ""
    probabilities: dict[str, float] = field(default_factory=dict)
    backend: str = "heuristic"
    ms: float = 0.0
    error: str = ""
    source: str = ""

    @property
    def confidence(self) -> float:
        return float(self.probabilities.get(self.choice, 0.0))

    def as_dict(self) -> dict[str, Any]:
        return {"choice": self.choice, "confidence": round(self.confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()},
                "backend": self.backend, "source": self.source or self.backend,
                "ms": round(self.ms, 1), "error": self.error}


@dataclass
class Score:
    """The answer to a `score` question: a position on an ordered scale."""

    value: float = 0.0              # 0..1 down the criteria list
    index: float = 0.0              # expected index over the criteria
    criteria: list[str] = field(default_factory=list)
    backend: str = "heuristic"
    ms: float = 0.0
    error: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"value": round(self.value, 4), "index": round(self.index, 3),
                "criteria": list(self.criteria), "backend": self.backend,
                "source": self.source or self.backend, "ms": round(self.ms, 1),
                "error": self.error}


class LayaBackend:
    """What every backend must answer — the whole surface Aura depends on."""

    name = "base"

    def decide(self, transcript: str, skill: str, args: dict, why: str = "") -> Decision:
        raise NotImplementedError

    # -- optional, used by the planner and the element picker ---------------- #

    def decide_many(self, transcript: str,
                    cases: Sequence[tuple[str, dict, str]]) -> list[Decision]:
        """Gate several actions of one request. Backends may batch them."""
        return [self.decide(transcript, skill, args, why) for skill, args, why in cases]

    def choose(self, state: Any, instructions: str, options: dict[str, str]) -> Choice:
        raise NotImplementedError

    def score(self, state: Any, instructions: str, criteria: list[str]) -> Score:
        raise NotImplementedError

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def ready(self) -> bool:
        return True

    def warmup(self) -> bool:
        return True

    def load(self) -> Any:
        return None

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "ready": self.ready, "error": ""}


# --------------------------------------------------------------------------- #
# The real backend — `laya.Router`, the published API                          #
# --------------------------------------------------------------------------- #


def laya_available() -> bool:
    """Is the `laya` package importable? (Cheap: does not import torch.)"""
    try:
        return importlib.util.find_spec("laya") is not None
    except (ImportError, ValueError):  # a broken install is not "available"
        return False


#: The checkpoint directory bundled with the repository — a real Laya
#: checkpoint (native `rl_agent_config.json` + `model.safetensors` format)
#: trained on Aura's navigation domain by scripts/train_navigation_laya.py.
BUNDLED_CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "assets" / "models" / "aura-nav-laya"


def _is_checkpoint_dir(path: Path) -> bool:
    """A usable native Laya checkpoint has its config and its weights."""
    return (path / "rl_agent_config.json").is_file() and (path / "model.safetensors").is_file()


def resolve_checkpoint_dir(cfg) -> tuple[str, str]:
    """Pick the local checkpoint directory to load, and say why.

    Resolution order (first hit wins):
      1. `laya.adapter_dir`     — the user's fine-tuned checkpoint
      2. `laya.checkpoint_dir`  — any explicit native checkpoint
      3. `$AURA_LAYA_CHECKPOINT`
      4. the bundled navigation checkpoint
    Returns `("", reason)` when nothing local is usable — the caller then
    lets the Router fall back to the hub default and logs the reason.
    """
    import os

    laya_cfg = getattr(cfg, "laya", None)
    for label, value in (
        ("laya.adapter_dir", str(getattr(laya_cfg, "adapter_dir", "") or "")),
        ("laya.checkpoint_dir", str(getattr(laya_cfg, "checkpoint_dir", "") or "")),
        ("AURA_LAYA_CHECKPOINT", os.environ.get("AURA_LAYA_CHECKPOINT", "")),
        ("bundled", str(BUNDLED_CHECKPOINT_DIR)),
    ):
        if not value:
            continue
        path = Path(value).expanduser()
        if _is_checkpoint_dir(path):
            return str(path), label
        if label != "bundled":
            log.warning("laya: %s points at %s but it is not a usable checkpoint "
                        "(needs rl_agent_config.json + model.safetensors) — ignored",
                        label, path)
    return "", "no local checkpoint — using the hub default"


def _noul(answer: Any, default: float = 0.0) -> float:
    """Read a `noul` probability out of whatever shape the answer arrived in."""
    if isinstance(answer, dict):
        for key in ("noul", "probability", "yes", "value", "score"):
            if key in answer:
                return _clamp(answer[key])
        return default
    return _clamp(answer, default)


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class RealLayaBackend(LayaBackend):
    """Adapter over `laya.Router`. One file to fix if the upstream API moves.

    Verified against the published interface — `Router(preload=…)` plus
    `predict(state, questions)` with `choice` / `score` / `noul` questions and
    a `routing` key on the result. Both gate questions are asked in the *same*
    `predict` call, so a whole action is judged in one forward pass.

    Checkpoints: the Router is pointed at a *local* checkpoint directory when
    one is resolved (`resolve_checkpoint_dir`) — the bundled Aura navigation
    checkpoint by default — and falls back to the hub default otherwise.
    """

    name = "laya"

    def __init__(self, device: str = "", model: str = "", adapter_dir: str = "",
                 checkpoint_dir: str = "", preload: bool = False,
                 max_loaded: int = 2) -> None:
        self.device = device or None
        self.model = model or None
        self.adapter_dir = adapter_dir or ""
        self.checkpoint_dir = checkpoint_dir or ""
        self.preload = bool(preload)
        self.max_loaded = max(1, int(max_loaded))
        self._router: Any = None
        self._lock = threading.RLock()
        self.version = ""

    # -- loading ------------------------------------------------------------ #

    def load(self) -> Any:
        """Import `laya` and build the Router. First call may download weights."""
        with self._lock:
            if self._router is not None:
                return self._router
            try:
                import laya  # type: ignore[import-not-found]  # optional dependency
            except Exception as exc:  # pragma: no cover - depends on the host
                raise LayaError("import", exc) from exc
            self.version = getattr(laya, "__version__", "")
            kwargs: dict[str, Any] = {"preload": self.preload,
                                      "max_loaded": self.max_loaded}
            if self.device:
                kwargs["device"] = self.device
            # A local checkpoint directory (fine-tune, an explicit checkpoint,
            # or the bundled navigation model) stands in for the English
            # checkpoint the router would otherwise fetch from the hub.
            local = self.adapter_dir or self.checkpoint_dir
            if local:
                kwargs["models"] = {"english": local}
                kwargs["default"] = "english"
            elif self.model:
                kwargs["default"] = self.model
            try:
                router = laya.Router(**kwargs)
            except Exception as exc:
                raise LayaError("Router()", exc) from exc
            self._router = router
            return router

    @property
    def ready(self) -> bool:
        return self._router is not None

    def warmup(self) -> bool:
        """Bring the model the router will actually use into memory.

        With `preload = true` the Router loads every checkpoint up front. The
        default is one trivial question instead: it downloads and builds only
        the checkpoint the router routes to, which is ~421M parameters rather
        than the whole family — the difference between a menu-bar app and a
        memory hog.
        """
        try:
            router = self.load()
            if self.preload:
                return True
            router.predict({"request": "warm-up"},
                           {"ready": {"type": "noul",
                                      "instructions": "Is this a warm-up request?"}})
        except Exception as exc:
            log.error("laya: warm-up failed — %s\n%s", describe_exception(exc),
                      traceback_text(exc, limit=12))
            raise LayaError("warm-up", exc) from exc
        return True

    def unload(self) -> bool:
        """Release the checkpoints (memory-constrained machines, idle policy)."""
        with self._lock:
            router, self._router = self._router, None
        if router is None:
            return False
        unload = getattr(router, "unload", None)
        if callable(unload):
            try:
                unload()
            except Exception as exc:  # pragma: no cover - best effort
                log.warning("laya: unload failed — %s", describe_exception(exc))
                return False
        return True

    # -- asking ------------------------------------------------------------- #

    def _predict(self, state: Any, questions: dict[str, Any],
                 model: str | None = None) -> dict[str, Any]:
        router = self.load()
        kwargs = {"model": model} if model else {}
        try:
            result = router.predict(state, questions, **kwargs)
        except Exception as exc:
            raise LayaError("predict", exc) from exc
        if not isinstance(result, dict) or "answers" not in result:
            raise LayaError("predict", f"unexpected result shape {type(result).__name__}: "
                                       f"{str(result)[:120]}")
        return result

    def _predict_many(self, states: list[Any], questions: dict[str, Any]) -> list[dict[str, Any]]:
        """One batched pass when the installed version supports it.

        `predict_batch` collates every state into shared forward passes; a
        version without it falls back to sequential `predict`, which is still
        correct, just not batched. This is what keeps an N-action plan (and a
        16-way element pick) at one model call instead of N.
        """
        router = self.load()
        batch = getattr(router, "predict_batch", None)
        if callable(batch) and len(states) > 1:
            try:
                # Router.predict_batch takes heterogeneous request dictionaries,
                # not ``(states, questions)``.  The latter happened to work with
                # our fake router but raises in every released Laya 0.3 version,
                # silently turning each compound command into N forward passes.
                requests = [{"state": state, "questions": questions} for state in states]
                results = batch(requests)
                if isinstance(results, list) and len(results) == len(states):
                    return results
                raise LayaError("predict_batch", f"got {type(results).__name__} of length "
                                                 f"{len(results) if hasattr(results, '__len__') else '?'}")
            except LayaError:
                raise
            except Exception as exc:
                log.warning("laya: predict_batch failed, falling back to per-state predict — %s",
                            describe_exception(exc))
        return [self._predict(state, questions) for state in states]

    @staticmethod
    def _state(transcript: str, skill: str, args: dict, why: str = "") -> str:
        return json.dumps({"request": transcript,
                           "action": {"skill": skill, "args": args, "rationale": why}})

    def decide(self, transcript: str, skill: str, args: dict, why: str = "") -> Decision:
        return self.decide_many(transcript, [(skill, args, why)])[0]

    def decide_many(self, transcript: str,
                    cases: Sequence[tuple[str, dict, str]]) -> list[Decision]:
        if not cases:
            return []
        states = [self._state(transcript, skill, args, why) for skill, args, why in cases]
        started = time.perf_counter()
        results = self._predict_many(states, GATE_QUESTIONS)
        elapsed = (time.perf_counter() - started) * 1000.0
        share = elapsed / max(1, len(results))
        out: list[Decision] = []
        for result in results:
            answers = result.get("answers", {}) if isinstance(result, dict) else {}
            routing = result.get("routing", {}) if isinstance(result, dict) else {}
            out.append(Decision(
                match=_noul(answers.get("match")),
                destructive=_noul(answers.get("destructive")),
                backend="laya", source="real", ms=share,
                model=str(routing.get("model", "") or ""),
                routing=str(routing.get("reason", "") or ""),
            ))
        return out

    def choose(self, state: Any, instructions: str, options: dict[str, str]) -> Choice:
        """`choice`: which of the closed set of options does this state mean?"""
        if not options:
            return Choice(choice="", backend=self.name, source="real")
        questions = {"pick": {"type": "choice", "instructions": instructions,
                              "criteria": dict(options)}}
        started = time.perf_counter()
        result = self._predict(state, questions)
        elapsed = (time.perf_counter() - started) * 1000.0
        answer = (result.get("answers", {}) or {}).get("pick", {}) or {}
        probabilities: dict[str, float] = {}
        choice = ""
        if isinstance(answer, dict):
            probabilities = {str(k): _clamp(v)
                             for k, v in (answer.get("probabilities") or {}).items()}
            choice = str(answer.get("choice", "") or "")
        return Choice(choice=choice, probabilities=probabilities, backend=self.name,
                      source="real", ms=elapsed)

    def score(self, state: Any, instructions: str, criteria: list[str]) -> Score:
        """`score`: where does this state sit on an ordered scale?"""
        if not criteria:
            return Score(criteria=[], backend=self.name, source="real")
        questions = {"level": {"type": "score", "instructions": instructions,
                               "criteria": list(criteria)}}
        started = time.perf_counter()
        result = self._predict(state, questions)
        elapsed = (time.perf_counter() - started) * 1000.0
        answer = (result.get("answers", {}) or {}).get("level", {}) or {}
        index = 0.0
        if isinstance(answer, dict):
            raw = answer.get("score", answer.get("value", 0.0))
            try:
                index = float(raw)
            except (TypeError, ValueError):
                index = 0.0
        span = max(1, len(criteria) - 1)
        return Score(value=_clamp(index / span), index=index, criteria=list(criteria),
                     backend=self.name, source="real", ms=elapsed)

    def status(self) -> dict[str, Any]:
        router = self._router
        loaded: list[str] = []
        if router is not None:
            try:
                loaded = list(router.loaded())
            except Exception:  # pragma: no cover - introspection must never raise
                loaded = []
        return {"backend": self.name, "ready": self.ready, "version": self.version,
                "device": self.device or "auto", "model": self.model or "auto",
                "adapter_dir": self.adapter_dir, "checkpoint_dir": self.checkpoint_dir,
                "loaded": loaded, "preload": self.preload, "error": ""}


# --------------------------------------------------------------------------- #
# Heuristic backend — deterministic, explainable, always available             #
# --------------------------------------------------------------------------- #

_DESTRUCTIVE_SKILLS = {
    "system.empty_trash": 0.97,
    "system.sleep": 0.75,
    "system.quit_app": 0.55,
    "system.start_recording": 0.6,
    "clipboard.set_text": 0.25,
    "system.toggle_dnd": 0.2,
    "ax.click": 0.12,          # clicks are usually safe; the label decides
    "ax.type_into": 0.08,
}
_DESTRUCTIVE_ARGS = re.compile(
    r"(rm\s+-rf|/etc/|/system|diskutil|sudo|format|erase|shutdown|reboot|drop\s+table"
    r"|\bdelete\b|\bempty\b|\bpurchase\b|\bcheckout\b|\bpay\b|\bsubmit\b|\bpublish\b"
    r"|\bsend\b|\binvite\b|\bshare\b|\bsign ?up\b|\bregister\w*\b|\bsubscrib\w*\b)",
    re.IGNORECASE,
)
_SHARED_ARG_KEYS = {"to", "recipient", "email", "share", "post"}
_WORD = re.compile(r"[a-z0-9']+")
_STOP = {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "my", "me",
         "please", "is", "it", "this", "that", "with", "at", "by", "do", "does"}


def _tokens(text: str, keep_stop: bool = False) -> set[str]:
    words = {w for w in _WORD.findall(str(text).lower()) if len(w) > 1}
    return words if keep_stop else (words - _STOP)


def _overlap(query: str, text: str) -> float:
    """Deterministic token overlap, 0..1 — the offline stand-in for a probability."""
    q, t = _tokens(query), _tokens(text)
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


class HeuristicBackend(LayaBackend):
    """The same three answers, from explicit rules.

    Deterministic, instant, and honest about being a fallback: it is what runs
    when `laya` is not installed, not loaded yet, or has just failed. It scores
    *worse* than the real model on ambiguous requests by construction — which
    is exactly why every decision it makes is labelled `backend="heuristic"`.
    """

    name = "heuristic"

    # -- the gate ----------------------------------------------------------- #

    def decide(self, transcript: str, skill: str, args: dict, why: str = "") -> Decision:
        started = time.perf_counter()
        destructive = _DESTRUCTIVE_SKILLS.get(skill, 0.05)
        arg_blob = json.dumps(args)
        if _DESTRUCTIVE_ARGS.search(arg_blob) or _DESTRUCTIVE_ARGS.search(str(why)):
            destructive = max(destructive, 0.9)
        if any(k in args for k in _SHARED_ARG_KEYS):
            destructive = max(destructive, 0.7)

        # Match: does the request mention any meaningful token of the args/skill?
        skill_tail = skill.split(".")[-1].replace("_", " ").lower()
        tokens = set(skill_tail.split()) | {
            str(v).lower().replace("_", " ") for v in args.values() if isinstance(v, str)
        }
        tokens = {t for t in tokens if len(t) > 2}
        req = transcript.lower()
        if not tokens:
            match = 0.7
        else:
            hits = sum(1 for tok in tokens if tok in req)
            match = 0.55 + 0.4 * (hits / len(tokens))
        return Decision(match=min(match, 0.99), destructive=destructive,
                        backend=self.name, source="heuristic",
                        ms=(time.perf_counter() - started) * 1000.0)

    # -- choice / score ------------------------------------------------------ #

    def choose(self, state: Any, instructions: str, options: dict[str, str]) -> Choice:
        """Token overlap between the state and each option's description.

        `state` may be a string, a list of strings, or a dict — the caller
        decides what the model sees, so the fallback flattens it the same way.
        """
        started = time.perf_counter()
        text = _flatten(state)
        query = f"{_flatten(instructions)} {text}".strip()
        scores: dict[str, float] = {}
        for label, description in options.items():
            base = _overlap(text, f"{label} {description}")
            nudge = 0.15 * _overlap(query, _flatten(description) or _flatten(label))
            scores[label] = _clamp(min(0.98, base * 0.85 + nudge))
        best = max(scores, key=lambda k: scores[k]) if scores else ""
        if best and scores[best] == 0.0:
            best = ""
        return Choice(choice=best, probabilities=scores, backend=self.name,
                      source="heuristic", ms=(time.perf_counter() - started) * 1000.0)

    def score(self, state: Any, instructions: str, criteria: list[str]) -> Score:
        """Pick the criterion whose wording overlaps the state most."""
        started = time.perf_counter()
        text = _flatten(state)
        if not criteria:
            return Score(criteria=[], backend=self.name, source="heuristic")
        scores = [_overlap(text, criterion) for criterion in criteria]
        index = float(max(range(len(scores)), key=lambda i: scores[i])) if any(scores) else 0.0
        span = max(1, len(criteria) - 1)
        return Score(value=_clamp(index / span), index=index, criteria=list(criteria),
                     backend=self.name, source="heuristic",
                     ms=(time.perf_counter() - started) * 1000.0)


def _flatten(value: Any) -> str:
    """Any state shape → one line of text, deterministically.

    Dict *keys* are dropped: "request", "action" and "args" are the envelope
    we wrap the state in, not words the user said, and counting them would
    dilute every overlap score.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


# --------------------------------------------------------------------------- #
# The gate — real Laya when it is ready, honest fallback when it is not        #
# --------------------------------------------------------------------------- #


class LayaGate(LayaBackend):
    """The one backend the rest of Aura talks to.

    Responsibilities, in order of importance:

    1. **Never block a session.** If the real backend is not loaded yet, or
       takes longer than `call_budget_seconds`, the heuristic answers *now*
       and the record explains why.
    2. **Load in the background.** `Router(preload=True)` may download
       checkpoints and takes seconds; that must not be paid by the user's
       first command nor by engine start-up.
    3. **Be loud about failure.** Each distinct failure is logged once with a
       traceback, `status()` reports it, and every fallback `Decision` carries
       the error string so the UI can show the user what went wrong.
    """

    name = "laya"

    def __init__(self, real: LayaBackend, fallback: LayaBackend | None = None, *,
                 call_budget_seconds: float = DEFAULT_CALL_BUDGET_SECONDS,
                 autoload: bool = True, on_fallback: Callable[[str], None] | None = None) -> None:
        self.real = real
        self.fallback = fallback or HeuristicBackend()
        self.call_budget_seconds = max(0.05, float(call_budget_seconds))
        self.on_fallback = on_fallback
        self.last_error = ""
        self.last_error_at = 0.0
        self.decisions = 0
        self.fallbacks = 0
        self.total_ms = 0.0
        self.timeouts = 0
        self._lock = threading.RLock()
        self._loading = False
        self._degraded_until = 0.0
        self._pool: _futures.ThreadPoolExecutor | None = None
        if autoload:
            self.warmup()

    # -- loading ------------------------------------------------------------- #

    @property
    def ready(self) -> bool:
        try:
            return bool(self.real.ready)
        except Exception:
            return False

    def warmup(self) -> bool:
        """Start (once) the background load. Returns True if already usable."""
        if self.ready:
            return True
        with self._lock:
            if self._loading:
                return False
            self._loading = True

        def work() -> None:
            try:
                ok = self.real.warmup()
            except Exception as exc:                     # pragma: no cover - defensive
                ok = False
                log.error("laya: background load raised — %s", describe_exception(exc))
            finally:
                with self._lock:
                    self._loading = False
            if ok:
                status = self.real.status()
                log.info("laya: ready — version=%s device=%s model=%s",
                         status.get("version") or "?", status.get("device") or "auto",
                         status.get("loaded") or status.get("model") or "auto")
            else:
                # `real.status()` carries the error when the backend recorded one.
                detail = self.real.status().get("error") or self.last_error or "load failed"
                self._note_failure(str(detail))

        threading.Thread(target=work, name="aura-laya-load", daemon=True).start()
        return False

    # -- failure bookkeeping -------------------------------------------------- #

    def _note_failure(self, detail: str) -> None:
        with self._lock:
            first_time = detail != self.last_error
            self.last_error = detail
            self.last_error_at = time.time()
        if first_time:
            log.error("laya: the real backend failed — %s. The offline gate answers instead; "
                      "run `python -m aura laya-check` for a full report.", detail)
        if self.on_fallback is not None:
            try:
                self.on_fallback(detail)
            except Exception:  # pragma: no cover - a listener must never matter
                pass

    def _call_allowed(self) -> bool:
        return time.monotonic() >= self._degraded_until

    def _degrade(self, seconds: float) -> None:
        with self._lock:
            self._degraded_until = max(self._degraded_until, time.monotonic() + seconds)

    def _guarded(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a model call under a wall-clock deadline.

        Executor threads are used because a forward pass is foreign code: a
        wedged model must not be able to hold the engine's loop.
        """
        with self._lock:
            if self._pool is None:
                # Two workers: one live call, one spare slot so an abandoned
                # call cannot block the next session's decision.
                self._pool = _futures.ThreadPoolExecutor(max_workers=2,
                                                         thread_name_prefix="aura-laya-call")
            pool = self._pool
        future = pool.submit(fn, *args)
        try:
            return future.result(timeout=self.call_budget_seconds)
        except _futures.TimeoutError as exc:
            future.cancel()
            self.timeouts += 1
            self._degrade(DEGRADE_COOLDOWN_SECONDS)
            raise LayaError("timeout", f"no answer within {self.call_budget_seconds:.2f}s") from exc

    # -- the gate ------------------------------------------------------------ #

    def decide(self, transcript: str, skill: str, args: dict, why: str = "") -> Decision:
        return self.decide_many(transcript, [(skill, args, why)])[0]

    def decide_many(self, transcript: str,
                    cases: Sequence[tuple[str, dict, str]]) -> list[Decision]:
        if not cases:
            return []
        reason = self._unavailable_reason()
        if reason:
            return self._fallback_many(transcript, cases, reason)
        try:
            decisions = self._guarded(self.real.decide_many, transcript, list(cases))
            decisions = list(decisions)
            if len(decisions) != len(cases):
                raise LayaError("decide_many",
                                f"expected {len(cases)} decisions, got {len(decisions)}")
        except Exception as exc:
            detail = describe_exception(exc)
            log.error("laya: gate call failed — %s\n%s", detail, traceback_text(exc, limit=12))
            self._note_failure(detail)
            return self._fallback_many(transcript, cases, detail)
        with self._lock:
            self.decisions += len(decisions)
            self.total_ms += sum(d.ms for d in decisions)
        return decisions

    def _unavailable_reason(self) -> str:
        """Why the real backend can't answer right now ("" when it can)."""
        if not self.ready:
            self.warmup()
            return self.last_error or "checkpoints still loading"
        if not self._call_allowed():
            return self.last_error or "temporarily degraded after a timeout"
        return ""

    def _fallback_many(self, transcript: str, cases: Sequence[tuple[str, dict, str]],
                       reason: str) -> list[Decision]:
        out: list[Decision] = []
        for skill, args, why in cases:
            decision = self.fallback.decide(transcript, skill, args, why)
            decision.error = reason
            decision.source = "fallback"
            out.append(decision)
        with self._lock:
            self.fallbacks += len(out)
        return out

    # -- choice / score ------------------------------------------------------ #

    def choose(self, state: Any, instructions: str, options: dict[str, str]) -> Choice:
        reason = self._unavailable_reason()
        if not reason:
            try:
                choice = self._guarded(self.real.choose, state, instructions, options)
                with self._lock:
                    self.decisions += 1
                    self.total_ms += choice.ms
                if choice.confidence > 0.0 or choice.choice in options:
                    return choice
                reason = choice.error or "the model returned no usable choice"
            except Exception as exc:
                reason = describe_exception(exc)
                log.error("laya: choice call failed — %s\n%s", reason,
                          traceback_text(exc, limit=12))
                self._note_failure(reason)
        choice = self.fallback.choose(state, instructions, options)
        choice.error = reason
        choice.source = "fallback"
        with self._lock:
            self.fallbacks += 1
        return choice

    def score(self, state: Any, instructions: str, criteria: list[str]) -> Score:
        reason = self._unavailable_reason()
        if not reason:
            try:
                score = self._guarded(self.real.score, state, instructions, criteria)
                with self._lock:
                    self.decisions += 1
                    self.total_ms += score.ms
                return score
            except Exception as exc:
                reason = describe_exception(exc)
                log.error("laya: score call failed — %s\n%s", reason,
                          traceback_text(exc, limit=12))
                self._note_failure(reason)
        score = self.fallback.score(state, instructions, criteria)
        score.error = reason
        score.source = "fallback"
        with self._lock:
            self.fallbacks += 1
        return score

    # -- introspection -------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        try:
            real = self.real.status()
        except Exception as exc:  # pragma: no cover - introspection must never raise
            real = {"backend": getattr(self.real, "name", "?"), "ready": False,
                    "error": describe_exception(exc)}
        with self._lock:
            avg = (self.total_ms / self.decisions) if self.decisions else 0.0
            out = {
                "backend": self.name if self.ready else f"{self.name} (unavailable)",
                "ready": self.ready,
                "loading": self._loading,
                "degraded": not self._call_allowed(),
                "decisions": self.decisions,
                "fallbacks": self.fallbacks,
                "timeouts": self.timeouts,
                "avg_ms": round(avg, 1),
                "error": self.last_error,
                "call_budget_seconds": self.call_budget_seconds,
                "real": real,
                "fallback": self.fallback.status(),
            }
        return out

    def unload(self) -> bool:
        """Drop the model (idle policy). The next question reloads it."""
        unload = getattr(self.real, "unload", None)
        if not callable(unload):
            return False
        try:
            dropped = bool(unload())
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("laya: unload failed — %s", describe_exception(exc))
            return False
        if dropped:
            with self._lock:
                self._degraded_until = 0.0
            log.info("laya: checkpoints released (%s decisions, %s fallbacks so far)",
                     self.decisions, self.fallbacks)
        return dropped

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- #
# Selection                                                                    #
# --------------------------------------------------------------------------- #


def build_backend(cfg, bus=None):
    """Choose the gate's backend and *log the choice and its reason*.

    Previously this swallowed every failure with `except Exception: pass`, which
    is how a broken Laya could run for weeks with nobody noticing. Now the
    decision is explicit: a missing package, a bad config value, or a deferred
    load all say so, in the log the user can open.
    """
    laya_cfg = getattr(cfg, "laya", None)
    mode = str(getattr(laya_cfg, "backend", "auto") or "auto").lower()
    fallback = HeuristicBackend()

    if mode in ("heuristic", "off", "none"):
        log.warning("laya: backend=%s in config — using the offline gate deliberately", mode)
        return fallback
    if not getattr(laya_cfg, "enabled", True):
        log.warning("laya: disabled in config — running the offline gate")
        return fallback
    if not laya_available():
        log.error("laya: the `laya` package is not importable — running the offline gate. "
                  "Install it with `pip install laya` (Aura's Setup panel can do this too).")
        return fallback

    checkpoint_dir, checkpoint_source = resolve_checkpoint_dir(cfg)
    real = RealLayaBackend(
        device=str(getattr(laya_cfg, "device", "") or ""),
        model=str(getattr(laya_cfg, "model", "") or ""),
        # The resolved directory is the single source of truth for load(); a
        # broken path configured by the user was already reported and skipped.
        adapter_dir=checkpoint_dir if checkpoint_source == "laya.adapter_dir" else "",
        checkpoint_dir=checkpoint_dir,
        preload=bool(getattr(laya_cfg, "preload", False)),
        max_loaded=int(getattr(laya_cfg, "max_loaded", 2) or 2),
    )
    budget = getattr(laya_cfg, "call_budget_seconds", DEFAULT_CALL_BUDGET_SECONDS)
    gate = LayaGate(real, fallback,
                    call_budget_seconds=float(budget or DEFAULT_CALL_BUDGET_SECONDS),
                    autoload=True)
    log.info("laya: real backend selected (device=%s, checkpoint=%s [%s], budget=%.2fs) — "
             "loading checkpoints in the background",
             real.device or "auto", checkpoint_dir or "hub default", checkpoint_source,
             gate.call_budget_seconds)
    return gate


# --------------------------------------------------------------------------- #
# Example buffer — the fine-tune dataset that grows by itself                  #
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS laya_examples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  transcript TEXT NOT NULL,
  skill TEXT NOT NULL,
  args_json TEXT NOT NULL,
  outcome TEXT NOT NULL,             -- auto | confirmed | corrected | cancelled
  match_label REAL NOT NULL,         -- 0..1 supervision for Q1
  destructive_label REAL NOT NULL,   -- 0..1 supervision for Q2
  weight REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_examples_ts ON laya_examples(ts);
"""


class ExampleBuffer:
    """SQLite-backed supervision set for the Laya fine-tune.

    Label semantics (kept deliberately simple and honest):
      auto/confirmed  → match 1.0, destructive as scored
      corrected       → match 0.0 (the action was NOT what the user meant)
      cancelled       → match 0.0, destructive 1.0 (user refused it)
    """

    OUTCOMES = ("auto", "confirmed", "corrected", "cancelled")

    def __init__(self, db_path: Path | str) -> None:
        self._lock = threading.RLock()   # written from the loop, read from HTTP
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        # Performance pragmas — WAL mode for concurrent read/write;
        # NORMAL sync + memory temp store cut disk I/O dramatically.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA cache_size=-2000")   # 2 MB cache
        self._db.execute("PRAGMA temp_store=MEMORY")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def record(self, transcript: str, skill: str, args: dict, outcome: str,
               match: float, destructive: float, weight: float = 1.0) -> None:
        if outcome not in self.OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r} (expected {self.OUTCOMES})")
        payload = json.dumps(args)
        with self._lock:
            self._record(transcript, skill, payload, outcome, match, destructive, weight)

    def _record(self, transcript, skill, args_json, outcome, match, destructive, weight):
        self._db.execute(
            "INSERT INTO laya_examples (ts, transcript, skill, args_json, outcome,"
            " match_label, destructive_label, weight) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), transcript, skill, args_json, outcome,
             match, destructive, weight),
        )
        self._db.commit()

    def stats(self) -> dict:
        with self._lock:
            return self._stats()

    def _stats(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*), SUM(outcome='corrected'), SUM(outcome='cancelled'),"
            " SUM(outcome='confirmed') FROM laya_examples"
        ).fetchone()
        return {"total": row[0] or 0, "corrected": row[1] or 0,
                "cancelled": row[2] or 0, "confirmed": row[3] or 0}

    def export_jsonl(self, path: Path | str, min_examples: int = 1) -> int:
        """Write the fine-tune file consumed by scripts/nightly_laya.py."""
        rows = self._db.execute(
            "SELECT transcript, skill, args_json, match_label, destructive_label, weight"
            " FROM laya_examples ORDER BY ts"
        ).fetchall()
        if len(rows) < min_examples:
            return 0
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        return self._export(out, rows)

    def _export(self, out: Path, rows: Iterable) -> int:
        count = 0
        with out.open("w") as fh:
            for transcript, skill, args_json, match, destructive, weight in rows:
                state = json.dumps({"request": transcript, "action": {
                    "skill": skill, "args": json.loads(args_json)}})
                fh.write(json.dumps({
                    "state": state,
                    "questions": [
                        {"question": Q_MATCH, "answer": {"no": 1.0 - match, "yes": match}},
                        {"question": Q_DESTRUCTIVE,
                         "answer": {"no": 1.0 - destructive, "yes": destructive}},
                    ],
                    "weight": weight,
                }) + "\n")
                count += 1
        return count
