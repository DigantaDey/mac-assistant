"""Memory + example buffer: local, plain, inspectable."""

from __future__ import annotations

import json

from aura.laya import ExampleBuffer
from aura.memory import Memory


class TestMemory:
    def test_roundtrip_event(self, tmp_path):
        mem = Memory(tmp_path / "m.sqlite3")
        mem.record_event("open spotify", {"actions": []}, "Opening Spotify.", "ok", 1234)
        evs = mem.recent_events()
        assert len(evs) == 1
        assert evs[0]["transcript"] == "open spotify"
        assert evs[0]["total_ms"] == 1234
        assert evs[0]["outcome"] == "ok"

    def test_search(self, tmp_path):
        mem = Memory(tmp_path / "m.sqlite3")
        mem.record_event("play bailamos by enrique", {"actions": []}, "", "ok", 5)
        mem.record_event("open spotify", {"actions": []}, "", "ok", 5)
        assert any("bailamos" in t for t in mem.search("bailamos"))

    def test_preferences(self, tmp_path):
        mem = Memory(tmp_path / "m.sqlite3")
        mem.set_preference("music app", "Spotify")
        mem.set_preference("music app", "Apple Music")  # upsert
        assert mem.get_preference("music app") == "Apple Music"

    def test_extract_preference(self, tmp_path):
        mem = Memory(tmp_path / "m.sqlite3")
        assert mem.extract_preference("my editor is Zed") == ("my editor", "Zed")
        assert mem.extract_preference("standups are at 9:30") == ("standups", "9:30")


class TestExampleBuffer:
    def test_record_and_stats(self, tmp_path):
        buf = ExampleBuffer(tmp_path / "m.sqlite3")
        buf.record("open spotify", "system.open_app", {"app": "Spotify"}, "auto", 1.0, 0.05, 1.0)
        buf.record("empty the trash", "system.empty_trash", {}, "cancelled", 0.0, 1.0, 2.0)
        stats = buf.stats()
        assert stats["total"] == 2
        assert stats["cancelled"] == 1

    def test_export_jsonl_shape(self, tmp_path):
        buf = ExampleBuffer(tmp_path / "m.sqlite3")
        buf.record("open spotify", "system.open_app", {"app": "Spotify"},
                   "confirmed", 1.0, 0.05, 1.0)
        out = tmp_path / "laya.jsonl"
        n = buf.export_jsonl(out)
        assert n == 1
        row = json.loads(out.read_text().splitlines()[0])
        assert json.loads(row["state"])["action"]["skill"] == "system.open_app"
        questions = {q["question"]: q["answer"] for q in row["questions"]}
        assert questions["Does this action match what the user asked for?"]["yes"] == 1.0
