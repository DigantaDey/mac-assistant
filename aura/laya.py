"""The Laya gate — the fast, calibrated decision layer between intent and action.

Laya (Apache-2.0, Convai Innovations) is a non-autoregressive "System 1"
decision model: it answers *typed questions* about a state with calibrated
probabilities in a single forward pass — ~33 ms via MLX, ~4 ms on the Neural
Engine. It never generates text, so it cannot hallucinate an instruction.
That makes it the right component to answer, for every proposed action:

    Q1  "Does this action match what the user asked for?"   (match → 0..1)
    Q2  "Is this action destructive or hard to undo?"       (destructive → 0..1)

Policy:  high match + low destructive ⇒ auto-approve
         anything else                ⇒ surface to the user (confirm/correct)

Bring-up honesty: the `laya` pip API is still settling, so the exact call
lives behind `LayaBackend` in one small adapter. When it is importable we use
it; otherwise a transparent, deterministic `HeuristicBackend` provides the
same answers from explicit rules — which is also what the demo profile and
tests run on. Swapping backends never touches the rest of the product.

Every gated decision is appended to an example buffer with the outcome the
user chose (auto-run / confirmed / corrected / cancelled). That buffer is
exactly the JSONL a Laya fine-tune consumes — see scripts/nightly_laya.py.
This is the feedback loop: Aura's reflexes literally retrain on your choices.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Decision:
    match: float              # P(action matches the request)
    destructive: float        # P(action is destructive/hard to undo)
    backend: str              # "laya" | "heuristic"

    @property
    def safe_confidence(self) -> float:
        return self.match * (1.0 - self.destructive)


class LayaBackend:
    def decide(self, transcript: str, skill: str, args: dict, why: str) -> Decision:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Real backend (used automatically when `laya` is importable, i.e. on the Mac) #
# --------------------------------------------------------------------------- #


class RealLayaBackend(LayaBackend):
    """Adapter over the `laya` package. One file to fix if the API moves."""

    def __init__(self, adapter_dir: str = "") -> None:
        from laya import Router  # type: ignore  # optional dependency

        self._router = Router(preload=True)
        self._adapter_dir = adapter_dir

    def decide(self, transcript: str, skill: str, args: dict, why: str) -> Decision:
        state = json.dumps({"request": transcript, "action": {"skill": skill, "args": args}})
        match = float(self._router.ask(
            state=state,
            question="Does this action match what the user asked for?",
            options=["no", "yes"],
        ).get("yes", 0.0))
        destructive = float(self._router.ask(
            state=json.dumps({"skill": skill, "args": args, "rationale": why}),
            question="Is this action destructive, irreversible, or shared beyond the device?",
            options=["no", "yes"],
        ).get("yes", 0.0))
        return Decision(match=match, destructive=destructive, backend="laya")


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
    r"|\bsend\b|\binvite\b|\bshare\b)",
    re.IGNORECASE,
)
_SHARED_ARG_KEYS = {"to", "recipient", "email", "share", "post"}


class HeuristicBackend(LayaBackend):
    def decide(self, transcript: str, skill: str, args: dict, why: str) -> Decision:
        destructive = _DESTRUCTIVE_SKILLS.get(skill, 0.05)
        arg_blob = json.dumps(args)
        if _DESTRUCTIVE_ARGS.search(arg_blob):
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
        return Decision(match=min(match, 0.99), destructive=destructive, backend="heuristic")


def build_backend(cfg) -> LayaBackend:
    if cfg.laya.backend in ("auto", "laya"):
        try:
            return RealLayaBackend(adapter_dir=cfg.laya.adapter_dir)
        except Exception:
            pass
    return HeuristicBackend()


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
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def record(self, transcript: str, skill: str, args: dict, outcome: str,
               match: float, destructive: float, weight: float = 1.0) -> None:
        assert outcome in self.OUTCOMES
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

    def _export(self, out: Path, rows) -> int:
        with out.open("w") as fh:
            for transcript, skill, args_json, match, destructive, weight in rows:
                state = json.dumps({"request": transcript, "action": {
                    "skill": skill, "args": json.loads(args_json)}})
                fh.write(json.dumps({
                    "state": state,
                    "questions": [
                        {"question": "Does this action match what the user asked for?",
                         "answer": {"no": 1.0 - match, "yes": match}},
                        {"question": "Is this action destructive, irreversible, or shared beyond the device?",
                         "answer": {"no": 1.0 - destructive, "yes": destructive}},
                    ],
                    "weight": weight,
                }) + "\n")
        return len(rows)
