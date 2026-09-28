"""System skills — apps, sound, display, focus, power.

Each skill is a thin, declared wrapper over one macOS surface. Best-effort by
design: if macOS refuses (TCC, missing helper), the result explains what to
grant instead of failing silently.
"""

from __future__ import annotations

import re
from typing import Any

from .base import Skill, SkillContext, SkillResult, SkillSpec


def _spec(name: str, description: str, *, args: dict | None = None,
          examples: list[str] | None = None, risk: str = "safe") -> SkillSpec:
    return SkillSpec(name=name, description=description, args=args or {},
                     examples=examples or [], default_risk=risk)


class OpenApp(Skill):
    spec = _spec("system.open_app", "Launch or focus an application by name",
                 args={"app": "string"}, examples=["open Spotify", "open Safari"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        app = str(args.get("app", "")).strip()
        if not app:
            return SkillResult(False, "Which app should I open?")
        ok, out = ctx.bridge.osascript(
            f'tell application "{app}" to activate')
        return SkillResult(ok, f"Opened {app}." if ok else f"Couldn't open {app}: {out}")


class QuitApp(Skill):
    spec = _spec("system.quit_app", "Quit an application by name (may lose unsaved work)",
                 args={"app": "string"}, examples=["quit Chrome"],
                 risk="confirm")

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        app = str(args.get("app", "")).strip()
        if not app:
            return SkillResult(False, "Which app should I quit?")
        ok, out = ctx.bridge.osascript(f'tell application "{app}" to quit')
        return SkillResult(ok, f"Quit {app}." if ok else f"Couldn't quit {app}: {out}")


class SetVolume(Skill):
    spec = _spec("system.set_volume", "Set the output volume (0-100)",
                 args={"level": "integer"}, examples=["set volume to 40"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        try:
            level = max(0, min(100, int(args.get("level", 50))))
        except (TypeError, ValueError):
            return SkillResult(False, "I need a number for the volume level.")
        ok, out = ctx.bridge.osascript(f"set volume output volume {level}")
        return SkillResult(ok, f"Volume at {level}." if ok else f"Couldn't set volume: {out}")


class Mute(Skill):
    spec = _spec("system.mute", "Mute the output sound", examples=["mute"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        ok, out = ctx.bridge.osascript("set volume output muted true")
        return SkillResult(ok, "Muted." if ok else f"Couldn't mute: {out}")


class BrightnessStep(Skill):
    """Display brightness via the media keys — best-effort, no private APIs."""

    def __init__(self, direction: str) -> None:
        self._direction = direction  # "up" | "down"
        self.spec = _spec(f"system.brightness_{direction}",
                          f"Nudge display brightness {direction}",
                          examples=[f"brightness {direction}"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        key = "144" if self._direction == "up" else "145"
        ok, out = ctx.bridge.osascript(
            'tell application "System Events" to key code ' + key)
        return SkillResult(ok, f"Brightness {self._direction}." if ok
                           else f"Couldn't change brightness (grant Accessibility): {out}")


class ToggleDND(Skill):
    spec = _spec("system.toggle_dnd", "Toggle Do Not Disturb / Focus via a Shortcuts hook",
                 examples=["do not disturb", "toggle focus"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        # macOS keeps moving Focus controls; the robust user-owned path is a
        # Shortcuts automation named "Toggle DND" the user creates once.
        ok, out = ctx.bridge.run(["shortcuts", "run", "Toggle DND"])
        if ok:
            return SkillResult(True, "Toggled Do Not Disturb.")
        return SkillResult(
            False,
            "Focus needs a one-time setup: create a Shortcuts shortcut named "
            "\u201cToggle DND\u201d that toggles your Focus, and I'll drive it.",
            data={"hint": "shortcuts://create-shortcut"})


class LockScreen(Skill):
    spec = _spec("system.lock_screen", "Lock the screen now", examples=["lock my screen"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        ok, out = ctx.bridge.run(
            ["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession",
             "-suspend"])
        return SkillResult(ok, "Locked." if ok else f"Couldn't lock: {out}")


class Sleep(Skill):
    spec = _spec("system.sleep", "Put the Mac to sleep", examples=["put my mac to sleep"],
                 risk="confirm")

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        ok, out = ctx.bridge.run(["pmset", "sleepnow"])
        return SkillResult(ok, "Sleeping now." if ok else f"Couldn't sleep: {out}")


class EmptyTrash(Skill):
    spec = _spec("system.empty_trash", "Permanently empty the Trash — irreversible",
                 examples=["empty the trash"], risk="confirm")

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        ok, out = ctx.bridge.osascript('tell application "Finder" to empty trash')
        return SkillResult(ok, "Trash emptied." if ok else f"Couldn't empty trash: {out}")


class StartRecording(Skill):
    spec = _spec("system.start_recording", "Start a screen recording (captures the screen)",
                 examples=["start a screen recording"], risk="confirm")

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        ok, out = ctx.bridge.osascript(
            'tell application "System Events" to keystroke "#" using {command down, shift down}')
        return SkillResult(ok, "Screen recording started." if ok else f"Couldn't record: {out}")


class Remember(Skill):
    """The visible edge of the learning loop — 'remember that …'."""

    spec = _spec("system.remember", "Store a durable fact or preference the user states",
                 args={"fact": "string"}, examples=["remember that my editor is Zed"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        fact = str(args.get("fact", "")).strip()
        if not fact:
            return SkillResult(False, "What should I remember?")
        key, value = ctx.memory.extract_preference(fact)
        ctx.memory.set_preference(key, value, source="user")
        return SkillResult(True, f"Noted: {key} is {value}.", data={"key": key, "value": value})


_NUM = re.compile(r"\d+")


def register_system_skills(registry: Any) -> None:  # SkillRegistry, typed loosely to avoid a cycle
    registry.register(OpenApp())
    registry.register(QuitApp())
    registry.register(SetVolume())
    registry.register(Mute())
    registry.register(BrightnessStep("up"))
    registry.register(BrightnessStep("down"))
    registry.register(ToggleDND())
    registry.register(LockScreen())
    registry.register(Sleep())
    registry.register(EmptyTrash())
    registry.register(StartRecording())
    registry.register(Remember())
