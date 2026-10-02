"""Form skills — dictate into any browser form, seamlessly.

The headline of the v0.5.1 "power" push:

  ax.read_form   what does this form ask? (field list + its buttons)
  ax.fill_form   "fill this form: name John, email j@x.com, then submit"
  ax.dictate     type the last thing you said into the *focused* field —
                 no tree read, no parse, one keystroke event: as fast as
                 a dictation app can be

Fill is grounded end to end: the on-screen AX tree decides which fields
exist, the parser decides which values the user gave, and anything
unmatched is reported instead of guessed. Filling is fast (no friction);
pressing the button that *sends* it is where Aura stops and asks.
"""

from __future__ import annotations

import re
from typing import Any

from ..formfill import FormField, plan_fill, scan_form
from ..picker import default_picker
from .base import Skill, SkillContext, SkillResult, SkillSpec

_FIELD_ROLES = ("textfield", "searchfield", "textarea", "combobox",
                "securetextfield", "popupbutton")

_SUBMIT_CANDIDATE = re.compile(
    r"^(submit|send|save|continue|sign ?up|register|complete|purchase|pay|apply|check ?out)$",
    re.IGNORECASE)


def _spec(name: str, description: str, *, args: dict | None = None,
          examples: list[str] | None = None, risk: str = "safe") -> SkillSpec:
    return SkillSpec(name=name, description=description, args=args or {},
                     examples=examples or [], default_risk=risk)


def _tree(ctx: SkillContext):
    return ctx.bridge.ax_tree()


def _match_assignments(explicit: list[dict], fields: list[FormField]) -> list:
    """Validate planner-supplied field values against the live form."""
    from ..formfill import FieldAssignment, _mention_tokens

    out = []
    for item in explicit or []:
        label = str(item.get("label", "")).strip()
        value = str(item.get("value", "")).strip()
        if not label or not value:
            continue
        want = _mention_tokens(label)
        target = next((f for f in fields if f.label.lower() == label.lower()), None)
        if target is None:
            target = next((f for f in fields if want and want & _mention_tokens(f.label)), None)
        if target is None or any(a.field is target for a in out):
            continue
        out.append(FieldAssignment(target, value, "explicit", 0.9))
    return out


# --------------------------------------------------------------------------- #
# The skills                                                                  #
# --------------------------------------------------------------------------- #

class AXReadForm(Skill):
    spec = _spec("ax.read_form",
                 "List the fields of the form on the frontmost screen, and its buttons",
                 examples=["what fields does this form have",
                           "what does this form ask for"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        tree = _tree(ctx)
        scan = scan_form(tree.root(), tree.front_app_name())
        if not scan.fields:
            return SkillResult(False,
                               "I don't see a form on this screen — put the page with "
                               "the fields in front of me.")
        names = ", ".join(f.label for f in scan.fields[:12])
        more = f" …and {len(scan.fields) - 12} more." if len(scan.fields) > 12 else ""
        btns = [b.label for b in scan.submit_buttons[:4]]
        tail = f" It ends with: {', '.join(btns)}." if btns else ""
        return SkillResult(
            True,
            f"In {scan.app}, the form asks for {names}{more}.{tail}",
            data={"fields": scan.labels(), "buttons": btns},
        )


class AXFillForm(Skill):
    spec = _spec("ax.fill_form",
                 "Fill the form on the frontmost screen from the user's dictated values",
                 args={"raw": "string (the dictation to parse)",
                       "fields": "list of {label, value} (only when already parsed)",
                       "submit": "button label to press afterwards, if any"},
                 examples=["fill this form: name John Smith, email john@smith.com",
                           "complete the signup form and press sign up",
                           "fill out the checkout with the details I just said"],
                 risk="safe")

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        tree = _tree(ctx)
        scan = scan_form(tree.root(), tree.front_app_name())
        if not scan.fields:
            return SkillResult(False,
                               "I don't see a form to fill on this screen — is the "
                               "page with the fields frontmost?")

        raw = str(args.get("raw", ""))
        assignments = _match_assignments(args.get("fields") or [], scan.fields)
        if not assignments and raw:
            # The deterministic grammar is the only parser — Aura has no LLM.
            assignments = plan_fill(raw, scan)
        if not assignments:
            names = ", ".join(f.label for f in scan.fields[:8])
            btns = ", ".join(b.label for b in scan.submit_buttons[:2])
            tail = f" It ends with {btns}." if btns else ""
            return SkillResult(
                False,
                f"I didn't match anything you said to the form. It asks for {names}."
                f"{tail} Try: fill this form — name John, email you at example dot com.")

        filled, missed = [], []
        for a in assignments:
            if a.field.node is not None and tree.insert(a.field.node, a.value):
                filled.append(f"{a.field.label}")
            else:
                missed.append(f"{a.field.label}")

        msg = (f"Filled {', '.join(filled)} in {scan.app}." if filled
               else f"I couldn't write into any of the fields in {scan.app}.")
        if missed:
            msg += f" Still empty: {', '.join(missed)}."

        submitted = ""
        if str(args.get("submit", "")).strip():
            picker = default_picker()
            res = picker.pick(str(args["submit"]).strip(), tree.root(),
                              want_roles=("button", "link"))
            if res.best is None:  # fall back to any plausible submit button
                res = picker.pick("submit", tree.root(), want_roles=("button", "link"))
            if res.best is not None and tree.press(res.best):
                submitted = f" Pressed “{res.best_label}”."
            else:
                submitted = " I couldn't find the button to press."
        elif any(_SUBMIT_CANDIDATE.match(b.label) for b in scan.submit_buttons):
            # The user didn't ask to press anything — never press for them.
            pass

        return SkillResult(
            bool(filled),
            msg + submitted,
            data={"filled": filled, "missed": missed,
                  "unfilled": [f.label for f in scan.fields
                               if f.label not in filled + missed]},
        )


class AXDictate(Skill):
    spec = _spec("ax.dictate",
                 "Type the dictated text into the currently focused field, instantly",
                 args={"text": "string"},
                 examples=["type 123 Main Street",
                           "dictate: hello there",
                           "just type hunter2"])

    async def execute(self, args: dict[str, Any], ctx: SkillContext) -> SkillResult:
        text = str(args.get("text", ""))
        if not text.strip():
            return SkillResult(False, "What should I type?")
        ok, _detail = ctx.bridge.keystroke(text)
        if not ok:
            return SkillResult(False, "I couldn't type that — is a field focused?")
        return SkillResult(True, f"Typed {text!r} into the focused field.",
                           data={"chars": len(text)})


def register_form_skills(registry) -> None:
    registry.register(AXReadForm())
    registry.register(AXFillForm())
    registry.register(AXDictate())
