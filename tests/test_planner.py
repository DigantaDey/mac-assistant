"""Planner: deterministic rule-layer behaviour (Aura's reflexes).

The tolerant JSON parsing these tests used to cover belonged to the LLM
planner, which is gone: Aura's brain is Laya, and Laya never generates text.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from aura.planner import MockPlanner


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
        the Laya planner gets the whole request to route."""
        plan = await MockPlanner("").plan("open notes and refactor the kernel", {})
        assert [a.skill for a in plan.actions] == ["system.open_app"]
        assert plan.complete is False

    async def test_a_fully_routable_chain_is_complete(self):
        plan = await MockPlanner("").plan("open notes and then close safari", {})
        assert [a.skill for a in plan.actions] == ["system.open_app", "system.quit_app"]
        assert plan.complete is True


class TestBrowserTargetedOpens:
    """'open X in <browser>' — the reported failure. The site must be
    separated from the browser; the trailing words must never leak into the
    URL (they used to produce `url="youtube.com in safari"`, which the gate
    rightly refused and the user experienced as "nothing happened")."""

    @pytest.mark.parametrize("text,url,browser", [
        ("open youtube.com in safari", "youtube.com", "Safari"),
        ("open youtube in safari", "youtube", "Safari"),
        ("open github.com in chrome", "github.com", "Google Chrome"),
        ("go to reddit.com in firefox", "reddit.com", "Firefox"),
        ("open reddit on edge", "reddit", "Microsoft Edge"),
    ])
    async def test_the_site_is_separated_from_the_browser(self, text, url, browser):
        plan = await MockPlanner("").plan(text, {})
        assert plan.actions, text
        assert plan.actions[0].skill == "browser.open_url", text
        assert plan.actions[0].args == {"url": url, "browser": browser}, text
        assert plan.complete is True

    async def test_trailing_context_never_pollutes_an_app_name(self):
        plan = await MockPlanner("").plan("open spotify on my mac", {})
        assert plan.actions[0].skill == "system.open_app"
        assert plan.actions[0].args == {"app": "Spotify"}

    async def test_the_url_stays_an_address_not_a_search(self):
        """The old capture yielded "youtube.com in safari", which the browser
        skill's `_ensure_url` demoted to a Google search of that string."""
        from aura.skills.browser import _ensure_url

        plan = await MockPlanner("").plan("open youtube.com in safari", {})
        url = _ensure_url(plan.actions[0].args["url"])
        assert url.startswith("https://"), url
        assert " " not in url, url

    async def test_plain_opens_are_untouched(self):
        plan = await MockPlanner("").plan("open safari", {})
        assert plan.actions[0].skill == "system.open_app"
        assert plan.actions[0].args == {"app": "Safari"}
