"""Browser skills — the web is where assistants earn their keep.

v0.1 drives the user's real browser (Safari/Chrome/Edge) via `open` +
AppleScript tab introspection: instant, permission-light, and it respects
whatever profile the user already lives in. Deeper DOM control (a MV3
extension + CDP for form-filling) is scaffolded in ROADMAP v0.4.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from .base import Skill, SkillContext, SkillResult, SkillSpec

SEARCH_URL = "https://www.google.com/search?q={q}"

POPULAR = {
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "maps": "https://maps.google.com",
    "calendar": "https://calendar.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "reddit": "https://www.reddit.com",
}


def _spec(name: str, description: str, *, args: dict | None = None,
          examples: list[str] | None = None, risk: str = "safe") -> SkillSpec:
    return SkillSpec(name=name, description=description, args=args or {},
                     examples=examples or [], default_risk=risk)


def _ensure_url(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        return raw
    if raw in POPULAR:
        return POPULAR[raw]
    if "." in raw and " " not in raw:
        return f"https://{raw}"
    return SEARCH_URL.format(q=urllib.parse.quote_plus(raw))


class OpenURL(Skill):
    spec = _spec("browser.open_url", "Open a website or URL in the default browser",
                 args={"url": "string"}, examples=["open github.com", "go to youtube"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        url = _ensure_url(str(args.get("url", "")))
        if not url or url == SEARCH_URL.format(q=""):
            return SkillResult(False, "Which site should I open?")
        ok, out = ctx.bridge.open_url(url)
        return SkillResult(ok, f"Opened {url}." if ok else f"Couldn't open {url}: {out}",
                           data={"url": url})


class SearchWeb(Skill):
    spec = _spec("browser.search", "Search the web in the default browser",
                 args={"query": "string"}, examples=["search for flights to Goa"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return SkillResult(False, "What should I search for?")
        url = SEARCH_URL.format(q=urllib.parse.quote_plus(query))
        ok, out = ctx.bridge.open_url(url)
        return SkillResult(ok, f"Searching for {query}." if ok else f"Couldn't search: {out}",
                           data={"url": url})


class ListTabs(Skill):
    spec = _spec("browser.list_tabs", "List the open tabs of the front browser window",
                 examples=["list my tabs"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        for browser, script in (
            ("Safari", 'tell application "Safari" to get {name, URL} of tabs of window 1'),
            ("Google Chrome",
             'tell application "Google Chrome" to get {title, URL} of tabs of active tab of window 1'),
        ):
            ok, out = ctx.bridge.osascript(script)
            if ok and out.strip():
                return SkillResult(True, f"{browser} tabs: {out[:400]}", data={"browser": browser})
        return SkillResult(False, "I couldn't read tabs — grant Automation permission "
                                  "for your browser in System Settings.")


class FocusTab(Skill):
    spec = _spec("browser.focus_tab", "Bring an already-open tab to the front",
                 args={"title": "string"}, examples=["switch to the github tab"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        title = str(args.get("title", "")).strip()
        if not title:
            return SkillResult(False, "Which tab?")
        script = (
            'tell application "Google Chrome" to set active tab index of window 1 to '
            f'(index of first tab of window 1 whose title contains "{title}")'
        )
        ok, out = ctx.bridge.osascript(script)
        return SkillResult(ok, f"Switched to {title}." if ok
                           else f"No tab matching {title!r}: {out}")


def register_browser_skills(registry: Any) -> None:
    registry.register(OpenURL())
    registry.register(SearchWeb())
    registry.register(ListTabs())
    registry.register(FocusTab())
