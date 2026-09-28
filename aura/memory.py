"""Memory — everything Aura notices about you, on your disk, in plain SQLite.

Tables:
  events          — one row per session (transcript, plan, outcome, latency)
  preferences     — durable facts ("music app is Spotify", "work mode means …")
  laya_examples   — supervision set for the decision-model fine-tune
                    (owned by aura.laya.ExampleBuffer, same database file)

FTS5 gives instant full-text search over history with zero extra services.
The whole file is human-readable with `sqlite3` — no opaque blobs, ever.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  transcript TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  reply TEXT DEFAULT '',
  outcome TEXT DEFAULT 'ok',          -- ok | cancelled | failed | blocked
  total_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS preferences (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL,
  source TEXT DEFAULT 'user'          -- user | inferred
);
"""


class Memory:
    def __init__(self, db_path: Path | str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # HTTP threads read this store; serialize everything through one lock.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        try:
            self._db.executescript(
                "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(transcript);"
                "CREATE TRIGGER IF NOT EXISTS events_fts_ins AFTER INSERT ON events BEGIN"
                " INSERT INTO events_fts(rowid, transcript) VALUES (new.id, new.transcript);"
                " END;"
            )
        except sqlite3.OperationalError:
            pass  # FTS5 unavailable → LIKE fallback in search()
        self._db.commit()

    # -- events ---------------------------------------------------------------

    def record_event(self, transcript: str, plan: dict, reply: str,
                     outcome: str, total_ms: int) -> int:
        with self._lock:
            return self._record_event(transcript, plan, reply, outcome, total_ms)

    def _record_event(self, transcript: str, plan: dict, reply: str,
                      outcome: str, total_ms: int) -> int:
        cur = self._db.execute(
            "INSERT INTO events (ts, transcript, plan_json, reply, outcome, total_ms)"
            " VALUES (?,?,?,?,?,?)",
            (time.time(), transcript, json.dumps(plan), reply, outcome, total_ms),
        )
        self._db.commit()
        return int(cur.lastrowid or 0)

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return self._recent_events(limit)

    def _recent_events(self, limit: int) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["plan"] = json.loads(d.pop("plan_json") or "{}")
            out.append(d)
        return out

    def search(self, query: str, limit: int = 5) -> list[str]:
        if not query.strip():
            return []
        with self._lock:
            return self._search(query, limit)

    def _search(self, query: str, limit: int) -> list[str]:
        try:
            rows = self._db.execute(
                "SELECT transcript FROM events_fts WHERE events_fts MATCH ?"
                " ORDER BY rank LIMIT ?", (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            rows = self._db.execute(
                "SELECT transcript FROM events WHERE transcript LIKE ? LIMIT ?",
                (like, limit),
            ).fetchall()
        return [r["transcript"] for r in rows]

    # -- preferences ------------------------------------------------------------

    def set_preference(self, key: str, value: str, source: str = "user") -> None:
        with self._lock:
            return self._set_preference(key, value, source)

    def _set_preference(self, key: str, value: str, source: str) -> None:
        self._db.execute(
            "INSERT INTO preferences (key, value, updated_at, source) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at, source=excluded.source",
            (key, value, time.time(), source),
        )
        self._db.commit()

    def get_preference(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
            "SELECT value FROM preferences WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def all_preferences(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM preferences").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def extract_preference(self, fact: str) -> tuple[str, str]:
        """'remember that my music app is Spotify' → ('music app', 'Spotify')."""
        _FILLERS = {"at", "to", "the", "on", "in", "a", "an", "always"}
        text = fact.strip().rstrip(".")
        lowered = text.lower()
        for sep in (" is ", " = ", " means ", " are "):
            if sep in lowered:
                idx = lowered.index(sep)
                key = text[:idx].strip().lower()
                words = text[idx + len(sep):].split()
                while words and words[0].lower().strip(",") in _FILLERS:
                    words = words[1:]
                return key, " ".join(words).strip() or text
        return "note", text
