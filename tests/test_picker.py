"""The element picker: coarse-to-fine over the mock AX window."""

from __future__ import annotations

from aura.ax import MockAXTree
from aura.picker import ElementPicker, HeuristicScorer, LayaScorer


def tree() -> MockAXTree:
    return MockAXTree()


class TestHeuristicScorer:
    def test_exact_phrase_wins(self):
        s = HeuristicScorer()
        root = tree().root()
        sign_in = [n for n in root.flat() if n.label == "Sign in"][0]
        static = [n for n in root.flat() if n.label.startswith("The world")][0]
        assert s.score_option("sign in", sign_in) > 0.9
        assert s.score_option("sign in", static) < 0.3

    def test_role_prior(self):
        s = HeuristicScorer()
        root = tree().root()
        save_btn = [n for n in root.flat() if n.label == "Save preferences"][0]
        # "save button" should beat "save" alone via the button prior
        assert s.score_option("save button", save_btn) >= s.score_option("save", save_btn)


class TestCoarseToFine:
    def test_finds_sign_in_across_chunks(self):
        root = tree().root()
        result = ElementPicker().pick("sign in", root)
        assert result.best is not None
        assert result.best.label == "Sign in"
        assert result.confidence >= 0.62

    def test_chunking_actual(self):
        root = tree().root()
        options = [n for n in root.flat() if n.label or n.value]
        assert len(options) > 16               # the mock window forces >1 chunk
        result = ElementPicker().pick("delete repository", root)
        assert result.chunks >= 2
        assert result.best is not None
        assert result.best.label == "Delete repository"

    def test_low_confidence_gives_suggestions(self):
        root = tree().root()
        result = ElementPicker().pick("quantum entangle the flux capacitor", root)
        assert result.best is None

    def test_role_filter_for_fields(self):
        root = tree().root()
        result = ElementPicker().pick("search", root,
                                      want_roles=("textfield", "searchfield"))
        assert result.best is not None
        assert result.best.role in ("textfield", "searchfield")
        assert "Search" in result.best.label

    def test_suggestions_ranked(self):
        root = tree().root()
        result = ElementPicker().pick("sign", root)
        assert result.best is not None or result.suggestions
        scores = [s for _, s in result.suggestions]
        assert scores == sorted(scores, reverse=True)


class TestLayaScorerShape:
    def test_laya_scorer_uses_backend(self):
        class FakeBackend:
            def __init__(self):
                self.calls = []

            def decide(self, transcript, skill, args, why):
                self.calls.append((skill, args))
                from aura.laya import Decision
                label = str(args.get("element", "")).lower()
                return Decision(match=0.95 if "sign in" in label else 0.1,
                                destructive=0.0, backend="fake")

        backend = FakeBackend()
        scorer = LayaScorer(backend)
        root = tree().root()
        result = ElementPicker(scorer, threshold=0.62).pick("click sign in", root)
        assert result.best is not None
        assert result.best.label == "Sign in"
        # every backend call was a typed question — never free text
        assert all("pick_element" in skill for skill, _ in backend.calls)
