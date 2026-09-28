"""Accessibility skills end-to-end: voice command → picker → element action."""

from __future__ import annotations

import asyncio

import pytest
from conftest import DemoStack


@pytest.fixture()
def orch(stack: DemoStack):
    return stack.build_orchestrator()


async def session_transcript(orch: object, text: str) -> str:
    """Run a session and return the final reply text."""
    sid = orch.bus.subscribe_async()
    task = asyncio.create_task(orch.submit_text(text))
    reply = ""
    while not task.done():
        ev = await asyncio.wait_for(orch.bus.get(sid), timeout=5)
        if ev.type == "reply":
            reply = ev.data.get("text", "")
    await task
    for ev in orch.bus.drain(sid):
        if ev.type == "reply":
            reply = ev.data.get("text", "")
    orch.bus.unsubscribe_async(sid)
    return reply


class TestAXClick:
    async def test_click_by_label(self, orch):
        reply = await session_transcript(orch, "click the sign in button")
        assert "Sign in" in reply
        assert "Safari" in reply  # the mock tree's app name
        actions = [entry for entry in orch.bridge.ax_tree().log]
        assert ("press", "Sign in") in actions

    async def test_click_destructive_requires_confirmation(self, orch):
        sid = orch.bus.subscribe_async()
        task = asyncio.create_task(orch.submit_text("click the delete repository button"))
        while True:
            ev = await asyncio.wait_for(orch.bus.get(sid), timeout=5)
            if ev.type == "proposal":
                break
        # nothing pressed yet
        assert not [e for e in orch.bridge.ax_tree().log if e[1] == "Delete repository"]
        orch.resolve_confirmation(ev.data["token"], "confirm")
        await asyncio.wait_for(task, timeout=5)
        assert ("press", "Delete repository") in orch.bridge.ax_tree().log
        orch.bus.unsubscribe_async(sid)

    async def test_click_unknown_asks_not_guesses(self, orch):
        reply = await session_transcript(orch, "click the flux capacitor button")
        task_reply = reply.lower()
        assert "couldn't find" in task_reply or "did you mean" in task_reply
        assert not orch.bridge.ax_tree().log  # nothing was pressed


class TestAXTypeInto:
    async def test_type_into_search_field(self, orch):
        reply = await session_transcript(orch, "type aura assistant into the search field")
        assert "Typed into" in reply
        tree = orch.bridge.ax_tree()
        assert any("insert" in action for action, _ in tree.log)
        field = [n for n in tree.root().flat() if n.label == "Search GitHub"][0]
        assert field.value == "aura assistant"


class TestAXReadScreen:
    async def test_read_screen_lists_controls(self, orch):
        reply = await session_transcript(orch, "what's on my screen?")
        assert "Safari shows:" in reply
        assert "Sign in" in reply


class TestSkillDeclarations:
    def test_ax_skills_in_catalog(self, stack: DemoStack):
        names = stack.registry.names()
        assert {"ax.click", "ax.type_into", "ax.read_screen"} <= names

    def test_ax_click_described_for_planner(self, stack: DemoStack):
        catalog = stack.registry.catalog_prompt()
        assert "ax.click" in catalog
        assert "by name" in catalog


class TestNoAppleScriptInjection:
    """Skill args come from the LLM (the user's words). A stray quote must not
    be able to break out of the AppleScript string literal."""


    def _script_for(self, stack: DemoStack, skill: str, args: dict) -> str:
        import asyncio as _aio

        from aura.skills.base import SkillContext

        ctx = SkillContext.__new__(SkillContext)
        ctx.bridge = stack.bridge
        ctx.config = stack.cfg
        ctx.memory = stack.memory
        skill_obj = stack.registry.get(skill)
        _aio.run(skill_obj.execute(args, ctx))
        # the last osascript this call issued
        scripts = [d for k, d in stack.bridge.calls if k == "osascript"]
        return scripts[-1]

    @staticmethod
    def _unescaped_quotes(script: str) -> int:
        return script.count('"') - script.count('\\"')

    @staticmethod
    def _outside_literals(script: str) -> str:
        """The script with everything inside string literals removed."""
        import re
        return re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', "", script)

    def test_app_name_quote_cannot_escape(self, stack: DemoStack):
        payload = 'X" & (do shell script "rm -rf ~") & "'
        script = self._script_for(stack, "system.open_app", {"app": payload})
        # the whole app name must sit inside ONE string literal, and nothing
        # from the payload may exist as a command outside it
        assert self._unescaped_quotes(script) == 2, script
        assert "do shell script" not in self._outside_literals(script), script

    def test_quit_app_quote_cannot_escape(self, stack: DemoStack):
        payload = 'Q" & (do shell script "id") & "'
        script = self._script_for(stack, "system.quit_app", {"app": payload})
        assert self._unescaped_quotes(script) == 2, script

    def test_clipboard_text_quote_cannot_escape(self, stack: DemoStack):
        script = self._script_for(stack, "clipboard.set_text",
                                  {"text": 'a" & (do shell script "id") & "b'})
        assert self._unescaped_quotes(script) == 2, script

    def test_focus_tab_title_quote_cannot_escape(self, stack: DemoStack):
        script = self._script_for(stack, "browser.focus_tab",
                                  {"title": 't" & (do shell script "id") & "'})
        # one literal for the app ("Google Chrome") + one for the title
        assert self._unescaped_quotes(script) == 4, script
        assert script.count(chr(92) + chr(34)) >= 2, script
