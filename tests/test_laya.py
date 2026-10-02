"""The Laya layer: heuristic gate parity + example plumbing."""

from __future__ import annotations

from aura.laya import HeuristicBackend


class TestHeuristicBackend:
    def test_safe_skill_scores_safe(self):
        d = HeuristicBackend().decide("open spotify", "system.open_app", {"app": "Spotify"}, "")
        assert d.destructive < 0.2
        assert d.match > 0.62

    def test_destructive_skill_flagged(self):
        d = HeuristicBackend().decide("empty the trash", "system.empty_trash", {}, "")
        assert d.destructive > 0.55

    def test_destructive_args_flagged(self):
        d = HeuristicBackend().decide("clean my disk", "system.open_app",
                                      {"app": "Terminal", "script": "diskutil eraseDisk"}, "")
        assert d.destructive > 0.8

    def test_unrelated_args_lower_match(self):
        d = HeuristicBackend().decide("tell me a joke", "system.set_volume", {"level": 3}, "")
        assert d.match < 0.62

    def test_backend_string(self):
        assert HeuristicBackend().decide("x", "y", {}, "").backend == "heuristic"


class TestBackendSelection:
    def test_falls_back_to_heuristic_without_laya(self, monkeypatch):
        import aura.laya as laya_mod

        class Cfg:
            class laya:
                backend = "auto"
                adapter_dir = ""
                checkpoint_dir = ""

        # The package being unavailable is exactly what laya_available() says.
        monkeypatch.setattr(laya_mod, "laya_available", lambda: False)
        backend = laya_mod.build_backend(Cfg())
        assert isinstance(backend, HeuristicBackend)
