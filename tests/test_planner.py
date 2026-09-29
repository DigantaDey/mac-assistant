"""Planner: tolerant parsing + deterministic mock behaviour."""

from __future__ import annotations

from typing import ClassVar

import pytest

from aura.planner import MockPlanner, extract_json_object, parse_plan


class TestJSONExtraction:
    def test_clean_json(self):
        assert extract_json_object('{"reply": "hi", "actions": []}') == {"reply": "hi", "actions": []}

    def test_fenced_json(self):
        text = 'Sure!\n```json\n{"reply": "ok"}\n```'
        assert extract_json_object(text) == {"reply": "ok"}

    def test_embedded_in_prose(self):
        text = 'Here is my plan: {"reply": "ok", "actions": [{"skill":"a.b","args":{"x":1}}]} hope that helps'
        obj = extract_json_object(text)
        assert obj and obj["actions"][0]["skill"] == "a.b"

    def test_nested_braces_inside_strings(self):
        text = '{"reply": "use { and } carefully", "actions": []}'
        assert extract_json_object(text)["reply"].startswith("use")

    def test_garbage(self):
        assert extract_json_object("no json here at all") is None


class TestParsePlan:
    def test_drops_malformed_actions(self):
        plan = parse_plan({"reply": "ok", "actions": [
            {"skill": "good.skill", "args": {"a": 1}},
            {"args": {}},                      # no skill → dropped
            "not a dict",                      # junk → dropped
            {"skill": "risky", "risk": "WEIRD"},  # bad risk → coerced to safe
        ]})
        assert len(plan.actions) == 2
        assert plan.actions[1].risk == "safe"

    def test_fallback_reply(self):
        plan = parse_plan(None)
        assert "rephrase" in plan.reply.lower()


class TestMockPlanner:
    @pytest.mark.parametrize("text,skill", [
        ("open spotify", "system.open_app"),
        ("Open Spotify and set volume to 30", "system.open_app"),
        ("set volume to 40", "system.set_volume"),
        ("mute", "system.mute"),
        ("empty the trash", "system.empty_trash"),
        ("search for the weather in Kolkata", "browser.search"),
        ("open github.com", "browser.open_url"),
        ("remember that my music app is Spotify", "system.remember"),
        ("list my tabs", "browser.list_tabs"),
    ])
    async def test_routes(self, text, skill):
        plan = await MockPlanner("").plan(text, {})
        assert plan.actions, text
        assert plan.actions[0].skill == skill

    async def test_unknown_gets_honest_reply(self):
        plan = await MockPlanner("").plan("refactor the kernel", {})
        assert not plan.actions
        assert "skill" in plan.reply.lower()

    async def test_destructive_flagged_confirm(self):
        plan = await MockPlanner("").plan("empty the trash", {})
        assert plan.actions[0].risk == "confirm"


class TestReflexCoverage:
    """The everyday commands people actually say.

    The rules answer these in microseconds so a model round-trip is never on
    the critical path. Every one of these used to fall through to "I don't
    have a skill for that yet" — or worse, to a confidently wrong action.
    """

    ROUTES: ClassVar = [
        # the same request, said the way people say it
        ("can you fire up youtube please", "browser.open_url"),
        ("launch notes", "system.open_app"),
        ("pull up calculator", "system.open_app"),
        ("switch to spotify", "system.open_app"),
        ("go to bbc.com", "browser.open_url"),
        ("kill safari", "system.quit_app"),
        ("shut down safari", "system.quit_app"),
        ("force quit safari", "system.quit_app"),
        ("record my screen", "system.start_recording"),
        ("look up airport lounges online", "browser.search"),
        ("google laya", "browser.search"),
        ("search the web for cats", "browser.search"),
        ("turn the screen down a notch", "system.brightness_down"),
        ("make the display brighter", "system.brightness_up"),
    ]

    @pytest.mark.parametrize("text,skill", ROUTES)
    async def test_routes(self, text, skill):
        plan = await MockPlanner("").plan(text, {})
        assert plan.actions, text
        assert plan.actions[0].skill == skill, text
        assert plan.source == "rules"
        assert plan.complete is True, text

    async def test_a_tab_is_not_an_application(self):
        """The old bug: "close this tab" reached the quit-app rule and became
        quit_app(app="This Tab"). A rule layer must fail by refusing."""
        for text in ["close this tab", "shut this tab", "next tab",
                     "gimme the next tab", "switch to the last tab"]:
            plan = await MockPlanner("").plan(text, {})
            assert not plan.actions, text
            assert not any(a.skill == "system.quit_app" for a in plan.actions), text
            assert "tab" in plan.reply.lower(), text

    async def test_search_drops_the_trailing_online(self):
        plan = await MockPlanner("").plan("look up airport lounges online", {})
        assert plan.actions[0].args["query"] == "airport lounges"

    async def test_a_url_is_lowercased(self):
        """_ensure_url (and its POPULAR table) expect a lowercase host."""
        plan = await MockPlanner("").plan("open GitHub.com", {})
        assert plan.actions[0].args["url"] == "github.com"

    @pytest.mark.parametrize("text,target", [
        ("open youtube", "youtube"),
        ("go to Gmail", "gmail"),
        ("pull up reddit please", "reddit"),
    ])
    async def test_popular_sites_never_route_to_osascript(self, text, target):
        """A popular site is not assumed to be an installed Mac app.

        AppleScript app resolution can wait on UI/permissions; browser.open_url
        is deterministic and returns inside the response budget.
        """
        plan = await MockPlanner("").plan(text, {})
        assert plan.actions[0].skill == "browser.open_url"
        assert plan.actions[0].args == {"url": target}

    async def test_a_partly_routable_chain_is_marked_incomplete(self):
        """"open notes and refactor the kernel": placing the first half and
        silently dropping the second is not an answer, so the plan says so and
        HybridPlanner hands the whole request to the model."""
        plan = await MockPlanner("").plan("open notes and refactor the kernel", {})
        assert [a.skill for a in plan.actions] == ["system.open_app"]
        assert plan.complete is False

    async def test_a_fully_routable_chain_is_complete(self):
        plan = await MockPlanner("").plan("open notes and then close safari", {})
        assert [a.skill for a in plan.actions] == ["system.open_app", "system.quit_app"]
        assert plan.complete is True
