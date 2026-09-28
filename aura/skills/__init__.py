from . import accessibility, browser, clipboard, forms, system
from .base import (
    DryRunBridge,
    MacBridge,
    Skill,
    SkillContext,
    SkillRegistry,
    SkillResult,
    SkillSpec,
)

__all__ = [
    "DryRunBridge", "MacBridge", "Skill", "SkillContext", "SkillRegistry",
    "SkillResult", "SkillSpec", "accessibility", "browser", "clipboard",
    "forms", "system",
]


def build_default_registry(bridge: MacBridge | None = None) -> SkillRegistry:
    """The full skill set: system, browser, clipboard, memory, accessibility,
    and forms (dictation-driven filling of any browser form)."""
    reg = SkillRegistry()
    system.register_system_skills(reg)
    browser.register_browser_skills(reg)
    clipboard.register_clipboard_skills(reg)
    accessibility.register_accessibility_skills(reg)
    forms.register_form_skills(reg)
    return reg
