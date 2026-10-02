"""Form filling — scan a browser form, map dictated values to its fields.

The whole point: the user looks at any browser form and says what they mean —
"fill this form: name John Smith, email john@smith.com, then submit" — and
Aura puts the words in the right boxes, in any browser, by label.

The parser is deliberately grounded: it never invents a value for a field the
user didn't mention, and it only ever writes to fields that are actually on
screen (the live AX tree is the source of truth, not the model's memory).

One brain, one path — and it is not an LLM:

  heuristic  — deterministic, instant, offline. Field-driven matching with a
               small grammar ("label is value", "label: value", "first field
               value", "fill this form: a, b, c"). This is what runs in the
               product: Aura's brain (Laya) routes and gates but never
               generates text, so a value is either in your words or it is
               not written at all.

The grammar is deliberately *grounded*: it only ever writes to fields that
are actually on screen (the live AX tree is the source of truth), and it only
ever writes a value that appears in your words. There is no model pass and
no free paraphrase — a field the grammar can't place is reported back, never
guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ax import AXNode

# Roles that can receive typed text, in any browser.
FIELD_ROLES = ("textfield", "searchfield", "textarea", "combobox",
               "securetextfield", "popupbutton")

# Buttons that end a form — "and then submit" finds one of these.
SUBMIT_RE = re.compile(
    r"\b(submit|send|save|continue|sign ?up|register|complete|purchase|pay|apply|check ?out|place ?order|order)\b",
    re.IGNORECASE,
)

# Spoken labels that mean the same field ("the mail" / "email").
_SYNONYMS: dict[str, frozenset[str]] = {
    "email": frozenset({"email", "e-mail", "mail"}),
    "phone": frozenset({"phone", "mobile", "number"}),
    "password": frozenset({"password", "pass", "pwd"}),
    "name": frozenset({"name"}),
}

_STOP = {"the", "a", "an", "please", "my", "our", "to", "of", "in", "on",
         "for", "with", "using", "and", "then", "is", "are", "was", "will",
         "should", "be", "as", "set", "type", "put", "enter", "make", "fill"}

_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3,
             "fifth": 4, "sixth": 5, "seventh": 6}


@dataclass
class FormField:
    label: str
    role: str
    value: str                    # current contents (usually "")
    node: AXNode = field(repr=False, default=None)


@dataclass
class FieldAssignment:
    field: FormField
    value: str
    how: str                      # label | ordinal | positional
    confidence: float = 1.0


@dataclass
class FormScan:
    app: str
    fields: list[FormField]
    submit_buttons: list[AXNode]

    def labels(self) -> list[str]:
        return [f.label for f in self.fields]


# --------------------------------------------------------------------------- #
# Scanning — what is this form?                                               #
# --------------------------------------------------------------------------- #

def scan_form(root: AXNode, app_name: str = "the frontmost app") -> FormScan:
    """Collect the fillable fields (and ending buttons) of the visible form."""
    fields: list[FormField] = []
    buttons: list[AXNode] = []
    for node in root.flat():
        if node is root or not node.label:
            continue
        if node.role in FIELD_ROLES:
            fields.append(FormField(label=node.label, role=node.role,
                                    value=node.value, node=node))
        elif node.role in ("button", "link") and SUBMIT_RE.search(node.label):
            buttons.append(node)
    return FormScan(app=app_name, fields=fields, submit_buttons=buttons)


# --------------------------------------------------------------------------- #
# The heuristic — fast, grounded, offline                                     #
# --------------------------------------------------------------------------- #

def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"\W+", s.lower()) if t and t not in _STOP}


def _mention_tokens(label: str) -> set[str]:
    """Tokens that count as a spoken mention of this field."""
    toks = _tokens(label)
    for word, syns in _SYNONYMS.items():
        if word in toks:
            toks |= syns
    return toks or {label.lower()}


_SPLIT_RE = re.compile(r"\s*(?:,|\band then\b|\band\b|\bthen\b)\s*")


def _split_segments(text: str) -> list[str]:
    parts = _SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


_LEAD_RE = re.compile(
    r"^(?:hey aura,?\s*)?(?:please\s+)?fill\s+(?:out|in)?\s*(?:this|that|the)?\s*"
    r"form(?:s)?\s*(?:with|using|:|-)?\s*", re.IGNORECASE)


def _strip_fill_lead(text: str) -> str:
    return _LEAD_RE.sub("", text.strip())


# Connectives between a spoken label and its value, most specific first.
_CONNECTIVES = (
    " is going to be ", " should be ", " will be ", " is set to ", " is ",
    " are ", " was ", "=", ": ", "- ", " to ", " as ", " being ", " of ",
    ", ", " ",
)


def _value_after_label(segment: str, label_tokens: set[str]) -> str | None:
    """Given a segment that mentions the field, extract the value.

    "name is John Smith" → "John Smith"     "email: j@x.com" → "j@x.com"
    "phone - 555 0100"   → "555 0100"       "first name John" → "John"

    We split the segment at every candidate connective, keep the splits whose
    head actually reads like the field's label (its tokens are label tokens,
    and it isn't a bare ordinal), and take the strongest. A segment that
    mentions the label but carries no value returns None — we never invent
    values.
    """
    seg = segment.strip()
    low = seg.lower()
    best: tuple[int, str] | None = None

    def consider(head: str, tail: str) -> None:
        nonlocal best
        tail = tail.strip(" .!?")
        if not head or not tail or not any(c.isalnum() for c in tail):
            return
        head_toks = _tokens(head)
        if not head_toks or not (head_toks & label_tokens):
            return
        if head_toks <= set(_ORDINALS):  # "first …" is the ordinal path's job
            return
        if head_toks <= label_tokens | _STOP and len(head_toks) <= 4:
            strength = len(head_toks & label_tokens) + 1  # a pure label phrase
        else:
            strength = len(head_toks & label_tokens)
        if best is None or strength > best[0]:
            best = (strength, tail)

    for conn in _CONNECTIVES:
        i = low.find(conn)
        while i != -1:
            consider(seg[:i], seg[i + len(conn):])
            i = low.find(conn, i + 1)

    if best is None:
        return None
    return best[1]


def plan_fill(transcript: str, scan: FormScan) -> list[FieldAssignment]:
    """Map a dictated transcript onto the scanned fields.

    Field-driven: for each visible field, find the segment that mentions it;
    the value is what follows. Ordinal mentions ("first field …") and a bare
    "fill this form: a, b, c" list fill the gaps. Whatever isn't mentioned
    stays empty — the caller reports it honestly.
    """
    text = transcript.strip()
    if not text or not scan.fields:
        return []

    # 1 — label-driven
    out: list[FieldAssignment] = []
    used_segments: set[int] = set()
    for fld in scan.fields:
        mention = _mention_tokens(fld.label)
        if not mention:
            continue
        best: tuple[int, str | None, float] | None = None
        for i, seg in enumerate(_split_segments(text)):
            if i in used_segments:
                continue
            seg_toks = _tokens(seg)
            if not (seg_toks & mention):
                continue
            overlap = len(seg_toks & mention) / len(mention)
            value = _value_after_label(_strip_fill_lead(seg), mention)
            if value is None:
                # The segment mentions the field but has no value in it —
                # e.g. "my name is on the other form". Skip, don't guess.
                continue
            score = 0.5 + 0.5 * overlap
            if best is None or score > best[2]:
                best = (i, value, score)
        if best:
            used_segments.add(best[0])
            out.append(FieldAssignment(fld, best[1], "label", round(best[2], 3)))

    # 2 — ordinal: "first field Alpha, second Beta"
    for fld in scan.fields:
        if any(a.field is fld for a in out):
            continue
        idx = scan.fields.index(fld)
        word = next((w for w, n in _ORDINALS.items() if n == idx), None)
        if not word:
            continue
        m = re.search(rf"\b{word}\b\s*(?:field|box|one|entry)?\s*(?:is|:|-|,| )+(.+?)(?=\s*(?:,|and|then|$))",
                      text, re.IGNORECASE)
        if m:
            value = m.group(1).strip(" .!?")
            if value and _tokens(value) and value.lower() not in _STOP:
                out.append(FieldAssignment(fld, value, "ordinal", 0.7))

    # 3 — bare list: "fill this form: Alpha, Beta, Gamma" → field order
    if not out:
        lead = _strip_fill_lead(text)
        items = [x.strip(" .!?") for x in re.split(r",", lead) if x.strip(" .!?")]
        if len(items) >= 2 and all(_tokens(x) for x in items[: len(scan.fields)]):
            for fld, value in zip(scan.fields, items):
                out.append(FieldAssignment(fld, value, "positional", 0.55))
            out = out[: len(scan.fields)]

    # Screen order, stable for the user to watch follow along.
    out.sort(key=lambda a: scan.fields.index(a.field))
    return out
