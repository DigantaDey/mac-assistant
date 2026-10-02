"""Live Laya: the real `laya` package answering through Aura's adapter.

Skipped when the `laya` package or a usable checkpoint is absent (the normal
unit suite runs on the fake/heuristic backends). When both are present — for
example with the bundled navigation checkpoint in `assets/models/` — these
tests run the genuine model end-to-end:

    RealLayaBackend → laya.Router → checkpoint forward pass → Aura's typed answers

This is the regression the product's "does the brain work?" question lives
in: `python -m aura laya-check` runs the same checks as a CLI.
"""

from __future__ import annotations

import pytest

from aura.laya import RealLayaBackend, laya_available, resolve_checkpoint_dir


class _Cfg:
    class laya:
        backend = "auto"
        enabled = True
        device = "cpu"
        model = ""
        adapter_dir = ""
        checkpoint_dir = ""
        preload = False
        max_loaded = 1
        call_budget_seconds = 30.0


def _backend():
    checkpoint, _source = resolve_checkpoint_dir(_Cfg())
    return RealLayaBackend(device="cpu", checkpoint_dir=checkpoint), checkpoint


def _skip_reason():
    if not laya_available():
        return "the `laya` package is not installed (pip install laya)"
    _checkpoint, _src = resolve_checkpoint_dir(_Cfg())
    if not _checkpoint:
        return ("no usable checkpoint (train one: "
                "python scripts/train_navigation_laya.py)")
    return ""


pytestmark = pytest.mark.skipif(bool(_skip_reason()), reason=_skip_reason())


@pytest.fixture(scope="module")
def backend():
    be, _checkpoint = _backend()
    be.load()
    return be


class TestLiveGate:
    @pytest.mark.parametrize("transcript,skill,args,match,destructive", [
        ("open spotify", "system.open_app", {"app": "Spotify"}, True, False),
        ("set volume to 30", "system.set_volume", {"level": 30}, True, False),
        ("empty the trash", "system.empty_trash", {}, True, True),
        ("what is the weather", "system.open_app", {"app": "Terminal"}, False, False),
    ])
    def test_gate_decisions(self, backend, transcript, skill, args, match, destructive):
        d = backend.decide(transcript, skill, args, "live test")
        assert d.backend == "laya"
        assert (d.match >= 0.5) is match, f"match={d.match:.3f} for {transcript!r}"
        assert (d.destructive >= 0.5) is destructive, \
            f"destructive={d.destructive:.3f} for {transcript!r}"

    def test_gate_is_one_forward_pass_per_action(self, backend):
        decisions = backend.decide_many(
            "open spotify and set volume to 30",
            [("system.open_app", {"app": "Spotify"}, ""),
             ("system.set_volume", {"level": 30}, "")])
        assert len(decisions) == 2
        assert all(d.backend == "laya" for d in decisions)


class TestLiveRouting:
    @pytest.mark.parametrize("transcript,expected", [
        ("open spotify", "system.open_app"),
        ("quiet the house", "system.toggle_dnd"),
        ("search for airport lounges", "browser.search"),
    ])
    def test_routes_the_skill_catalog(self, backend, transcript, expected):
        from aura.intent import LayaRouter
        from aura.skills import build_default_registry

        router = LayaRouter(backend, build_default_registry().specs())
        result = router.route(transcript)
        assert result.skill == expected, \
            f"{transcript!r} → {result.skill!r} (p={result.confidence:.2f}): {result.reason}"
        assert not result.error

    def test_chit_chat_routes_to_nothing(self, backend):
        from aura.intent import LayaRouter
        from aura.skills import build_default_registry

        router = LayaRouter(backend, build_default_registry().specs())
        result = router.route("tell me a joke")
        assert result.skill == "", f"chit-chat routed to {result.skill!r}"
