from .base import DryRunBridge, MacBridge, Skill, SkillContext, SkillRegistry, SkillResult, SkillSpec
from . import browser, clipboard, system

__all__ = [
    "DryRunBridge", "MacBridge", "Skill", "SkillContext", "SkillRegistry",
    "SkillResult", "SkillSpec", "browser", "clipboard", "system",
]


def build_default_registry(bridge: MacBridge | None = None) -> SkillRegistry:
    """The v0.1 skill set: system control, browser, clipboard, memory."""
    reg = SkillRegistry()
    system.register_system_skills(reg)
    browser.register_browser_skills(reg)
    clipboard.register_clipboard_skills(reg)
    return reg
