"""Planner: tolerant parsing + deterministic mock behaviour."""

from __future__ import annotations

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
