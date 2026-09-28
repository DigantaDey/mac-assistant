"""Accessibility skills — act inside any app, by label.

These are the v0.3 headline skills: Aura reads the frontmost app's real UI
tree, picks the element the user meant (coarse-to-fine, Laya-scored), and
presses/types with grounded confidence. Nothing here guesses at pixels: a
label comes from the app itself, and a below-threshold pick becomes an
honest "did you mean …" instead of a wrong click.
"""

from __future__ import annotations

from typing import Any

from .base import Skill, SkillContext, SkillResult, SkillSpec
from ..picker import PickResult, default_picker

_FIELD_ROLES = ("textfield", "searchfield", "textarea", "combobox")


def _spec(name: str, description: str, *, args: dict | None = None,
          examples: list[str] | None = None, risk: str = "safe") -> SkillSpec:
    return SkillSpec(name=name, description=description, args=args or {},
                     examples=examples or [], default_risk=risk)


def _tree(ctx: SkillContext):
    """The bridge decides which tree implementation reality gets."""
    return ctx.bridge.ax_tree()


def _describe_miss(result: PickResult, target: str) -> SkillResult:
    if result.suggestions:
        names = ", ".join(f"“{name}”" for name, _ in result.suggestions[:3])
        return SkillResult(
            False,
            f"I wasn't confident about “{target}” — did you mean {names}?",
            data={"suggestions": [n for n, _ in result.suggestions[:3]],
                  "confidence": result.confidence},
        )
    return SkillResult(False, f"I couldn't find anything matching “{target}”.")


class AXClick(Skill):
    spec = _spec("ax.click", "Click a button, link, or control in the frontmost app by name",
                 args={"target": "string"},
                 examples=["click the sign in button", "press save"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        target = str(args.get("target", "")).strip()
        if not target:
            return SkillResult(False, "What should I click?")
        tree = _tree(ctx)
        picker = default_picker()
        result = picker.pick(target, tree.root())
        if result.best is None:
            return _describe_miss(result, target)
        if not tree.press(result.best):
            return SkillResult(False, f"“{result.best_label}” isn't clickable right now.")
        return SkillResult(
            True,
            f"Pressed “{result.best_label}” in {tree.front_app_name()} "
            f"({int(result.confidence * 100)}% sure).",
            data={"label": result.best_label, "confidence": result.confidence},
        )


class AXTypeInto(Skill):
    spec = _spec("ax.type_into", "Type text into a named field of the frontmost app",
                 args={"target": "string", "text": "string"},
                 examples=["type hello into the search field"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        target = str(args.get("target", "")).strip()
        text = str(args.get("text", ""))
        if not target or not text.strip():
            return SkillResult(False, "Tell me what to type, and where.")
        tree = _tree(ctx)
        picker = default_picker()
        result = picker.pick(target, tree.root(), want_roles=_FIELD_ROLES)
        if result.best is None:
            loose = picker.pick(target, tree.root())
            if loose.best is not None and loose.confidence >= picker.threshold:
                result = loose
            else:
                return _describe_miss(result, target)
        if not tree.insert(result.best, text):
            return SkillResult(False, f"“{result.best_label}” isn't editable.")
        return SkillResult(
            True,
            f"Typed into “{result.best_label}” in {tree.front_app_name()}.",
            data={"label": result.best_label, "confidence": result.confidence},
        )


class AXReadScreen(Skill):
    spec = _spec("ax.read_screen", "Read out the visible controls of the frontmost app",
                 examples=["what's on my screen", "read my screen"])

    # Structural containers are layout, not content — skip them, and skip
    # long static text (paragraphs, not controls).
    _CONTAINERS = {"window", "group", "toolbar", "tabgroup", "list", "scrollarea",
                   "splittergroup", "layoutarea", "application", "unknown"}

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        tree = _tree(ctx)
        nodes = []
        for n in tree.root().flat():
            if n.role in self._CONTAINERS:
                continue
            label = n.label or n.value
            if not label:
                continue
            if n.role == "statictext" and len(label) > 40:
                continue
            nodes.append(n)
        if not nodes:
            return SkillResult(False, "I couldn't read this app's UI — it may not "
                                      "expose accessibility labels.")
        names = [n.label or n.value for n in nodes[:12]]
        more = f" …and {len(nodes) - 12} more." if len(nodes) > 12 else ""
        return SkillResult(
            True,
            f"{tree.front_app_name()} shows: " + ", ".join(names) + more,
            data={"count": len(nodes), "labels": names},
        )


def register_accessibility_skills(registry: Any) -> None:
    registry.register(AXClick())
    registry.register(AXTypeInto())
    registry.register(AXReadScreen())
