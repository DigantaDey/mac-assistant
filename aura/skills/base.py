"""Skills — the only way Aura touches your Mac.

A skill is a small, declared unit of capability:

  * `SkillSpec` — name, description, argument schema, example phrasings,
    default risk. The planner sees the catalog; the safety gate trusts the
    declaration; the UI renders it.
  * `execute()` — does one thing, returns a `SkillResult`. No hidden I/O.

Everything macOS-specific lives behind `MacBridge` so skills themselves stay
readable, and so the whole layer can run in `dry-run` mode (a Linux CI box,
the demo profile, or your first cautious week) where every action is logged
instead of executed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillSpec:
    name: str                    # dotted: "system.open_app"
    description: str             # planner-facing, one line
    args: dict[str, str] = field(default_factory=dict)     # arg → type hint
    examples: list[str] = field(default_factory=list)      # phrasings for the prompt
    default_risk: str = "safe"

    def catalog_line(self) -> str:
        args = ", ".join(f'{k}: {v}' for k, v in self.args.items()) or "none"
        ex = f' e.g. {" | ".join(chr(34) + e + chr(34) for e in self.examples[:2])}' if self.examples else ""
        return (f'- {self.name}({args}) — {self.description}.{ex} '
                f'[default risk: {self.default_risk}]')


@dataclass
class SkillResult:
    ok: bool
    message: str                 # human-facing, spoken or shown in the timeline
    data: dict[str, Any] = field(default_factory=dict)


class Skill:
    spec: SkillSpec

    async def execute(self, args: dict[str, Any], ctx: "SkillContext") -> SkillResult:
        raise NotImplementedError


@dataclass
class SkillContext:
    bridge: "MacBridge"          # macOS execution surface (real or dry-run)
    memory: Any                  # aura.memory.Memory
    config: Any                  # aura.config.Config


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.spec.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> set[str]:
        return set(self._skills)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def catalog_prompt(self) -> str:
        return "\n".join(s.spec.catalog_line() for s in self._skills.values())

    def specs(self) -> list[dict[str, Any]]:
        return [
            {"name": s.spec.name, "description": s.spec.description,
             "args": s.spec.args, "examples": s.spec.examples,
             "risk": s.spec.default_risk}
            for s in self._skills.values()
        ]


# --------------------------------------------------------------------------- #
# The macOS execution surface                                                  #
# --------------------------------------------------------------------------- #


class MacBridge:
    """Runs AppleScript/osascript + small CLI tools on a real Mac."""

    platform = "mac"

    def osascript(self, script: str) -> tuple[bool, str]:
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode == 0, (proc.stdout or proc.stderr).strip()

    def run(self, argv: list[str], timeout: int = 30) -> tuple[bool, str]:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or proc.stderr).strip()

    def open_url(self, url: str) -> tuple[bool, str]:
        return self.run(["open", url])


class DryRunBridge(MacBridge):
    """Logs exactly what would run on a Mac; executes nothing. Demo/CI/first run."""

    platform = "dry-run"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def osascript(self, script: str) -> tuple[bool, str]:
        self.calls.append(("osascript", script))
        return True, f"[dry-run] osascript: {script[:120]}"

    def run(self, argv: list[str], timeout: int = 30) -> tuple[bool, str]:
        self.calls.append(("run", " ".join(argv)))
        return True, f"[dry-run] {' '.join(argv[:4])}"

    def open_url(self, url: str) -> tuple[bool, str]:
        self.calls.append(("open", url))
        return True, f"[dry-run] open {url}"
