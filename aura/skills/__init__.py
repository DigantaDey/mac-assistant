from .base import DryRunBridge, MacBridge, Skill, SkillContext, SkillRegistry, SkillResult, SkillSpec
from . import accessibility, browser, clipboard, system

__all__ = [
    "DryRunBridge", "MacBridge", "Skill", "SkillContext", "SkillRegistry",
    "SkillResult", "SkillSpec", "accessibility", "browser", "clipboard", "system",
]


def build_default_registry(bridge: MacBridge | None = None) -> SkillRegistry:
    """The full skill set: system, browser, clipboard, memory, accessibility."""
    reg = SkillRegistry()
    system.register_system_skills(reg)
    browser.register_browser_skills(reg)
    clipboard.register_clipboard_skills(reg)
    accessibility.register_accessibility_skills(reg)
    return reg
