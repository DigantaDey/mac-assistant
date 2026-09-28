"""Clipboard skills — tiny, surprisingly high-value, perfect for voice."""

from __future__ import annotations

from typing import Any

from .base import Skill, SkillContext, SkillResult, SkillSpec, applescript_quote


class GetClipboard(Skill):
    spec = SkillSpec(
        name="clipboard.get_text",
        description="Read the current clipboard text and say it back",
        examples=["what's in my clipboard", "read my clipboard"],
    )

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        ok, out = ctx.bridge.run(["pbpaste"])
        text = out.strip() if ok else ""
        if not text:
            return SkillResult(True, "Your clipboard is empty.")
        spoken = text if len(text) <= 220 else text[:220] + "… and more."
        return SkillResult(True, f"Clipboard says: {spoken}", data={"text": text[:2000]})


class SetClipboard(Skill):
    spec = SkillSpec(
        name="clipboard.set_text",
        description="Copy given text to the clipboard",
        args={"text": "string"},
        examples=['copy "meet at 6" to the clipboard'],
    )

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        text = str(args.get("text", ""))
        if not text.strip():
            return SkillResult(False, "What should I copy?")
        # osascript keeps this dependency-free (pbcopy would need stdin plumbing).
        ok, out = ctx.bridge.osascript(
            f"set the clipboard to {applescript_quote(text[:500])}")
        return SkillResult(ok, "Copied." if ok else f"Couldn't copy: {out}")


def register_clipboard_skills(registry: Any) -> None:
    registry.register(GetClipboard())
    registry.register(SetClipboard())
