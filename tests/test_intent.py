"""Intent routing: deterministic arguments, a decision model choosing the skill.

The split this file pins down is the product's core claim: Laya *decides*
(choice/score questions with calibrated probabilities), deterministic code
*supplies facts* (the words the user actually said). Neither half guesses at
the other's job, and a skill whose arguments cannot be read is refused rather
than acted on.
"""

from __future__ import annotations

import pytest

from aura.intent import (
    NONE,
    LayaRouter,
    SkillSummary,
    coerce_specs,
    extract_args,
    normalize_request,
    reply_for,
    retrieval_score,
    shortlist,
    specs_from_catalog,
    volume_from_text,
)
from aura.laya import Choice, Decision, HeuristicBackend, LayaBackend, Score

# --------------------------------------------------------------------------- #
# Arguments — the half Laya cannot do                                          #
# --------------------------------------------------------------------------- #


class TestNormalization:
    @pytest.mark.parametrize("text,expected", [
        ("Hey Aura, can you please open Spotify?", "open spotify"),
        ("Could you set volume to 30 please", "set volume to 30"),
        ("   open notes   ", "open notes"),
        ("kindly mute", "mute"),
        ("mute, thanks!", "mute"),
    ])
    def test_politeness_is_reduced_away(self, text, expected):
        assert normalize_request(text) == expected


class TestArgumentExtraction:
    @pytest.mark.parametrize("skill,text,expected", [
        ("system.open_app", "open spotify", {"app": "Spotify"}),
        ("system.open_app", "launch activity monitor", {"app": "Activity Monitor"}),
        ("system.quit_app", "force quit safari", {"app": "Safari"}),
        ("system.set_volume", "set the volume to 30", {"level": 30}),
        ("system.set_volume", "turn it down a bit", {"level": 25}),
        ("system.set_volume", "make it louder", {"level": 80}),
        ("browser.open_url", "open github.com", {"url": "github.com"}),
        ("browser.open_url", "go to youtube", {"url": "youtube"}),
        ("browser.search", "look up airport lounges online", {"query": "airport lounges"}),
        ("browser.search", "search the web for cats", {"query": "cats"}),
        ("system.remember", "remember that my editor is Zed", {"fact": "my editor is Zed"}),
        ("ax.click", "click the Sign In button", {"target": "Sign In button"}),
        ("ax.type_into", "type Bob@Example.com into the email field",
         {"text": "Bob@Example.com", "target": "email field"}),
        ("ax.dictate", "type 123 Main Street", {"text": "123 Main Street"}),
        ("browser.focus_tab", "switch to the github tab", {"title": "github"}),
        ("clipboard.set_text", 'copy "meet at 6" to the clipboard', {"text": "meet at 6"}),
        ("system.mute", "mute", {}),
        ("system.empty_trash", "empty the trash", {}),
    ])
    def test_reads_what_the_user_said(self, skill, text, expected):
        assert extract_args(skill, text) == expected

    @pytest.mark.parametrize("skill,text", [
        ("system.open_app", "open youtube"),          # a site, not an app
        ("system.open_app", "open github.com"),       # a URL, not an app
        ("ax.type_into", "type something nice"),      # no target field
        ("browser.search", "search"),                 # no query
        ("system.remember", "remember this moment"),  # not a fact
        ("system.set_volume", "make it nicer"),       # no level at all
    ])
    def test_missing_arguments_are_none_not_empty(self, skill, text):
        assert extract_args(skill, text) is None

    def test_casing_survives_for_dictated_values(self):
        assert extract_args("ax.dictate", "dictate: Meet me at 6pm")["text"] == "Meet me at 6pm"

    @pytest.mark.parametrize("text,level", [
        ("volume 0", 0), ("volume 100", 100), ("volume 300", 100),
        ("maximum volume", 100), ("half volume", 50), ("quiet please", 25),
        ("mute it", 0),
    ])
    def test_volume_words_and_digits(self, text, level):
        assert volume_from_text(text) == level


# --------------------------------------------------------------------------- #
# Shortlisting — what the model is allowed to choose from                      #
# --------------------------------------------------------------------------- #


SPECS = [
    SkillSummary("system.open_app", "Launch or focus an application by name",
                 {"app": "string"}, ["open Spotify", "open Safari"]),
    SkillSummary("system.quit_app", "Quit an application by name", {"app": "string"},
                 ["quit Chrome"]),
    SkillSummary("system.set_volume", "Set the output volume (0-100)", {"level": "integer"},
                 ["set volume to 40"]),
    SkillSummary("system.mute", "Mute the output sound", {}, ["mute"]),
    SkillSummary("system.toggle_dnd", "Toggle Do Not Disturb / Focus", {},
                 ["do not disturb"]),
    SkillSummary("system.empty_trash", "Permanently empty the Trash", {}, ["empty the trash"]),
    SkillSummary("browser.search", "Search the web in the default browser",
                 {"query": "string"}, ["search for flights"]),
    SkillSummary("system.remember", "Store a durable fact or preference the user states",
                 {"fact": "string"}, ["remember that my editor is Zed"]),
]


class TestShortlist:
    def test_related_skills_rank_above_noise(self):
        picked = shortlist("open spotify", SPECS)
        assert picked[0].name == "system.open_app"

    def test_the_lexicon_finds_what_token_overlap_misses(self):
        """"quiet the house" shares no word with any skill description."""
        names = [spec.name for spec in shortlist("quiet the house", SPECS)]
        assert "system.toggle_dnd" in names
        assert "system.mute" in names

    def test_the_rules_choice_is_never_dropped(self):
        picked = shortlist("refactor the kernel", SPECS, must_include=["browser.search"], size=2)
        assert "browser.search" in [spec.name for spec in picked]

    def test_size_is_respected(self):
        assert len(shortlist("open spotify", SPECS, size=3)) <= 3

    def test_the_catalog_prompt_round_trips(self):
        from aura.skills import build_default_registry

        specs = coerce_specs(build_default_registry().specs())
        from_prompt = specs_from_catalog(build_default_registry().catalog_prompt())
        assert {s.name for s in specs} == {s.name for s in from_prompt}
        assert {s.name: s.risk for s in specs} == {s.name: s.risk for s in from_prompt}
        assert any("open Spotify" in " ".join(s.examples) for s in from_prompt)

    def test_retrieval_score_is_zero_for_unrelated_text(self):
        assert retrieval_score("refactor the kernel", SPECS[0]) == 0.0


# --------------------------------------------------------------------------- #
# The router                                                                   #
# --------------------------------------------------------------------------- #


class ScriptedBackend(LayaBackend):
    """A backend whose *answers* the test writes — the plumbing is what's under test."""

    name = "scripted"

    def __init__(self, picks=None, scores=None, error="", delay=0.0):
        self.picks = picks or {}
        self.scores = scores or {}
        self.error = error
        self.delay = delay
        self.choose_calls: list[dict] = []

    def choose(self, state, instructions, options):
        self.choose_calls.append({"instructions": instructions, "options": dict(options)})
        request = state["request"] if isinstance(state, dict) else str(state)
        label, probability = self.picks.get(request, ("", 0.0))
        return Choice(choice=label if label in options else "",
                      probabilities={label: probability} if label in options else {},
                      backend=self.name, source="real", error=self.error, ms=self.delay)

    def score(self, state, instructions, criteria):
        request = state["request"] if isinstance(state, dict) else str(state)
        index = self.scores.get(request, 5.0)
        return Score(value=index / 10.0, index=index, criteria=list(criteria),
                     backend=self.name, source="real", error=self.error, ms=self.delay)

    def decide(self, transcript, skill, args, why=""):
        return Decision(0.9, 0.05, backend=self.name, source="real", ms=self.delay)

    def score_calls(self):
        return self.scores


class TestLayaRouter:
    def make(self, **kwargs):
        return LayaRouter(ScriptedBackend(**kwargs), SPECS, min_confidence=0.3)

    def test_a_confident_pick_becomes_a_plan(self):
        router = self.make(picks={"quiet the house": ("system.toggle_dnd", 0.82)})
        result = router.route("quiet the house")
        assert result.skill == "system.toggle_dnd"
        assert result.args == {}
        assert result.confidence == pytest.approx(0.82)
        assert result.reply == "Toggling Do Not Disturb."
        assert result.risk == "safe"
        assert "laya chose" in result.reason or "scripted chose" in result.reason

    def test_the_question_offers_the_shortlist_and_a_none_option(self):
        backend = ScriptedBackend()
        LayaRouter(backend, SPECS).route("quiet the house")
        options = backend.choose_calls[0]["options"]
        assert NONE in options
        assert "system.toggle_dnd" in options
        assert len(options) <= 13
        assert "Do Not Disturb" in options["system.toggle_dnd"]

    def test_a_weak_pick_is_not_acted_on(self):
        router = self.make(picks={"quiet the house": ("system.toggle_dnd", 0.12)})
        result = router.route("quiet the house")
        assert result.skill == ""
        assert "threshold" in result.reason

    def test_none_means_none(self):
        router = self.make(picks={"book me a flight": (NONE, 0.91)})
        result = router.route("book me a flight")
        assert not result.routed
        assert "no skill" in result.reason

    def test_a_skill_without_readable_arguments_is_refused(self):
        """"remember my anniversary" is a remember-request with no fact in it."""
        router = self.make(picks={"remember my anniversary": ("system.remember", 0.8)})
        result = router.route("remember my anniversary")
        assert not result.routed
        assert "arguments" in result.reason

    def test_a_scored_level_fills_in_a_missing_number(self):
        """"turn it down a bit" has no digit — Laya scores the scale instead."""
        router = self.make(picks={"set the volume for the party": ("system.set_volume", 0.7)},
                           scores={"set the volume for the party": 2.0})
        result = router.route("set the volume for the party")
        assert result.skill == "system.set_volume"
        assert result.args == {"level": 20}

    def test_a_pick_outside_the_options_is_ignored(self):
        router = self.make(picks={"open spotify": ("not.a.skill", 0.99)})
        assert not router.route("open spotify").routed

    def test_a_backend_failure_returns_a_reason_not_an_exception(self):
        class Exploding(ScriptedBackend):
            def choose(self, state, instructions, options):
                raise RuntimeError("model went away")

        result = LayaRouter(Exploding(), SPECS).route("open spotify")
        assert not result.routed
        assert "RuntimeError: model went away" in result.error

    def test_the_offline_scorer_routes_the_obvious_cases(self):
        """Without the model the router still answers what token overlap can see."""
        router = LayaRouter(HeuristicBackend(), SPECS, min_confidence=0.3)
        assert router.route("open spotify").skill == "system.open_app"
        assert router.route("empty the trash").skill == "system.empty_trash"
        # …and refuses what it cannot: semantics are the model's job.
        assert not router.route("quiet the house").routed


class TestReplies:
    @pytest.mark.parametrize("skill,args,fragment", [
        ("system.open_app", {"app": "Spotify"}, "Opening Spotify"),
        ("system.set_volume", {"level": 30}, "30 percent"),
        ("browser.search", {"query": "cats"}, "cats"),
        ("system.empty_trash", {}, "permanently"),
        ("ax.click", {"target": "Send"}, ""),      # grounded skills speak for themselves
    ])
    def test_replies_are_short_and_specific(self, skill, args, fragment):
        assert fragment in reply_for(skill, args)
