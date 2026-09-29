"""Intent routing — which skill, and with which arguments.

Laya cannot generate text, so it cannot invent an argument. It *can* choose
among closed sets — that is exactly what routing needs:

    "Which skill should carry out this request?"   choice over the catalog
    "How loud should the volume be?"               score over 0…100

So the work is split by capability, and neither half pretends to do the
other's job:

  * **Deterministic code** normalizes the phrasing (politeness, punctuation),
    retrieves a shortlist of plausible skills and extracts the free-text
    arguments ("open **Spotify**", "type **123 Main St** into **address**").
  * **Laya decides**: one `choice` question over the shortlist, answered with
    calibrated probabilities in a single forward pass. Its verdict is used
    when the rules could not route the request at all, and it is *logged* when
    they could — a disagreement between the two layers is the most useful
    signal this product has for improving the rules.

Everything here is pure except `LayaRouter.route()`, which takes a backend
(`aura.laya.LayaGate`) so the same code runs on the real model, on the
heuristic fallback, and in tests.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .log import get_logger

log = get_logger("intent")

#: The "no skill" option every routing question carries. A closed set that
#: cannot say "none of these" forces a wrong answer.
NONE = "none"
NONE_TEXT = "nothing here matches the request; no action should be taken"

ROUTE_INSTRUCTIONS = ("Which single skill carries out the user's request? Choose the closest "
                      "real match, or 'none' when no skill truly matches.")

#: How many skills a routing question may offer. Laya's own benchmark notes
#: accuracy falls away past ~16 options, so the shortlist is capped and the
#: retrieval below is what keeps the right skill inside it.
SHORTLIST_SIZE = 12


# --------------------------------------------------------------------------- #
# The catalog                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class SkillSummary:
    """The part of a `SkillSpec` routing needs."""

    name: str
    description: str = ""
    args: dict[str, str] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)
    risk: str = "safe"

    @property
    def tail(self) -> str:
        """"system.open_app" → "open app" — the words people actually say."""
        return self.name.split(".")[-1].replace("_", " ")

    def option_text(self) -> str:
        """What Laya reads as this option's criterion."""
        parts = [f"{self.tail}: {self.description or self.name}"]
        if self.examples:
            parts.append("e.g. " + " | ".join(self.examples[:3]))
        return ". ".join(parts)

    def search_text(self) -> str:
        return f"{self.tail} {self.name.replace('.', ' ')} {self.description} " \
               f"{' '.join(self.examples)}".lower()


def coerce_specs(specs: Iterable[Any] | None) -> list[SkillSummary]:
    """Accept `SkillRegistry.specs()` dicts, `SkillSummary`s, or a mix."""
    out: list[SkillSummary] = []
    for spec in specs or []:
        if isinstance(spec, SkillSummary):
            out.append(spec)
        elif isinstance(spec, dict) and spec.get("name"):
            out.append(SkillSummary(
                name=str(spec["name"]),
                description=str(spec.get("description", "")),
                args=dict(spec.get("args") or {}),
                examples=[str(e) for e in (spec.get("examples") or [])],
                risk=str(spec.get("risk", "safe")),
            ))
    return out


def specs_from_catalog(catalog_prompt: str) -> list[SkillSummary]:
    """Rebuild `SkillSummary`s from `SkillRegistry.catalog_prompt()`.

    The registry can hand over its specs directly (`build_planner(..., specs=…)`);
    this is the fallback for callers that only have the prompt string, and it
    keeps the planner's public signature stable.
    """
    out: list[SkillSummary] = []
    line_re = re.compile(
        r"^-\s+(?P<name>[\w.]+)\((?P<args>.*?)\)\s+—\s+(?P<desc>.*?)"
        r"(?:\.\s+e\.g\.\s+(?P<examples>.*?))?\s*\[default risk:\s*(?P<risk>\w+)\]\s*$")
    for line in (catalog_prompt or "").splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        examples = [e.strip().strip('"') for e in (m.group("examples") or "").split("|") if e.strip()]
        args = {}
        for pair in (m.group("args") or "").split(","):
            if ":" in pair:
                key, _, value = pair.partition(":")
                args[key.strip()] = value.strip()
        out.append(SkillSummary(name=m.group("name"), description=m.group("desc").strip(),
                                args=args, examples=examples, risk=m.group("risk")))
    return out


# --------------------------------------------------------------------------- #
# Politeness, verbs, and the phrases people actually use                       #
# --------------------------------------------------------------------------- #

_OPEN_VERBS = r"(?:open|launch|fire up|pull up|bring up|switch to|start|run)"
_QUIT_VERBS = r"(?:quit|close|kill|force ?quit|terminate|shut down|exit)"
_VERB_ALIASES = {"launch": "open", "fire up": "open", "pull up": "open",
                 "bring up": "open", "start": "open", "run": "open",
                 "kill": "quit", "force quit": "quit", "terminate": "quit",
                 "shut down": "quit", "exit": "quit"}

_TLD = r"(?:com|org|net|io|dev|ai|co|app|edu|gov|me|tv|xyz|info)"
_URLISH = re.compile(rf"[\w-]+(?:\.[\w-]+)*\.{_TLD}(?:/[^\s]*)?", re.IGNORECASE)

# A small intent lexicon. Retrieval is deterministic on purpose: it decides
# *what Laya gets to choose from*, never what the answer is. Words here map to
# skills the way people mean them ("quiet" is DND + mute, not "silence").
INTENT_HINTS: dict[str, dict[str, float]] = {
    "open": {"system.open_app": 0.9, "browser.open_url": 0.8, "browser.focus_tab": 0.3},
    "launch": {"system.open_app": 0.9, "browser.open_url": 0.6},
    "quit": {"system.quit_app": 1.0},
    "close": {"system.quit_app": 0.9},
    "app": {"system.open_app": 0.4, "system.quit_app": 0.4},
    "volume": {"system.set_volume": 1.0, "system.mute": 0.8},
    "louder": {"system.set_volume": 1.0},
    "quieter": {"system.set_volume": 1.0, "system.mute": 0.5},
    "down": {"system.set_volume": 0.8, "system.brightness_down": 0.7},
    "up": {"system.set_volume": 0.8, "system.brightness_up": 0.7},
    "turn": {"system.set_volume": 0.5},
    "quiet": {"system.toggle_dnd": 1.0, "system.mute": 0.9, "system.set_volume": 0.7},
    "silence": {"system.mute": 1.0},
    "mute": {"system.mute": 1.0, "system.set_volume": 0.3},
    "sound": {"system.set_volume": 0.7, "system.mute": 0.7},
    "brightness": {"system.brightness_up": 1.0, "system.brightness_down": 1.0},
    "bright": {"system.brightness_up": 1.0},
    "dim": {"system.brightness_down": 1.0},
    "dark": {"system.brightness_down": 0.8, "system.toggle_dnd": 0.4},
    "focus": {"system.toggle_dnd": 1.0},
    "notifications": {"system.toggle_dnd": 1.0},
    "disturb": {"system.toggle_dnd": 1.0},
    "trash": {"system.empty_trash": 1.0},
    "bin": {"system.empty_trash": 0.9},
    "sleep": {"system.sleep": 1.0},
    "lock": {"system.lock_screen": 1.0},
    "record": {"system.start_recording": 1.0},
    "recording": {"system.start_recording": 1.0},
    "screenshot": {"system.start_recording": 0.5},
    "clipboard": {"clipboard.get_text": 0.8, "clipboard.set_text": 0.8},
    "paste": {"clipboard.get_text": 0.9, "clipboard.set_text": 0.6},
    "copy": {"clipboard.set_text": 1.0},
    "tab": {"browser.list_tabs": 0.9, "browser.focus_tab": 0.9},
    "tabs": {"browser.list_tabs": 1.0, "browser.focus_tab": 0.9},
    "search": {"browser.search": 1.0},
    "google": {"browser.search": 1.0},
    "browse": {"browser.open_url": 0.8, "browser.search": 0.6},
    "website": {"browser.open_url": 1.0},
    "url": {"browser.open_url": 1.0},
    "remember": {"system.remember": 1.0},
    "note": {"system.remember": 0.8},
    "click": {"ax.click": 1.0},
    "press": {"ax.click": 0.9},
    "tap": {"ax.click": 0.8},
    "type": {"ax.dictate": 0.9, "ax.type_into": 0.9},
    "dictate": {"ax.dictate": 1.0},
    "fill": {"ax.fill_form": 1.0},
    "form": {"ax.fill_form": 0.9, "ax.read_form": 0.9},
    "fields": {"ax.read_form": 1.0},
    "screen": {"ax.read_screen": 0.9, "system.start_recording": 0.4},
    "see": {"ax.read_screen": 0.7},
}

_NO_ARG_SKILLS = {"system.mute", "system.brightness_up", "system.brightness_down",
                  "system.toggle_dnd", "system.lock_screen", "system.sleep",
                  "system.empty_trash", "system.start_recording", "browser.list_tabs",
                  "clipboard.get_text", "ax.read_screen", "ax.read_form"}

_STOP = {"the", "a", "an", "my", "me", "please", "to", "of", "for", "and", "in", "on",
         "is", "it", "this", "that", "with", "at", "by", "do", "does", "can", "could",
         "you", "your", "would", "will", "up", "now", "then"}

_WORD = re.compile(r"[a-z0-9']+")


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text).lower()) if len(w) > 1 and w not in _STOP}


def normalize_request(text: str) -> str:
    """Lowercase, drop politeness — the same reduction the rule layer applies.

    "Can you please open Spotify?" and "open spotify" are one request.
    """
    t = str(text or "").lower().strip()
    t = re.sub(r"^(?:hey aura[,!.]?\s+)?(?:can|could|would|will) you (?:please )?",
               "", t)
    t = re.sub(r"^(?:please|pls|kindly)\s+", "", t)
    t = re.sub(r"[,!.]?\s*(?:please|thanks|thank you)\s*[.!]?$", "", t)
    return t.strip(" ,.!?")


def title_case(name: str) -> str:
    clean = " ".join(str(name or "").strip().split())
    brands = {"youtube": "YouTube", "github": "GitHub", "gmail": "Gmail",
              "whatsapp": "WhatsApp", "reddit": "Reddit"}
    return brands.get(clean.lower(), " ".join(w.capitalize() for w in clean.split()))


POPULAR_SITES = {"youtube", "github", "gmail", "google", "maps", "calendar",
                 "whatsapp", "reddit"}


# --------------------------------------------------------------------------- #
# Shortlisting — what Laya is allowed to choose from                           #
# --------------------------------------------------------------------------- #


def retrieval_score(transcript: str, spec: SkillSummary) -> float:
    """0..1: how plausibly this skill is about the request. Deterministic."""
    request = tokens(transcript)
    # The lexicon reads the raw words: "turn it down" is a volume request, and
    # "down" is a stopword for the overlap measure but not for this lookup.
    words = set(_WORD.findall(str(transcript).lower()))
    if not request:
        return 0.0
    text = spec.search_text()
    overlap = sum(1 for token in request if token in text) / len(request)
    hits = [weights.get(spec.name, 0.0) for word, weights in INTENT_HINTS.items()
            if word in words]
    hint = max(hits, default=0.0)
    return 0.65 * overlap + 0.35 * hint


def shortlist(transcript: str, specs: Sequence[SkillSummary], *,
              must_include: Iterable[str] = (), size: int = SHORTLIST_SIZE) -> list[SkillSummary]:
    """The ≤`size` skills worth asking Laya about, best first.

    Skills the rules already chose are always present: the model's job is to
    judge a candidate set, not to lose one the deterministic layer found.
    """
    by_name = {spec.name: spec for spec in specs}
    scored = sorted(((retrieval_score(transcript, spec), spec) for spec in specs),
                    key=lambda pair: (-pair[0], pair[1].name))
    chosen: list[SkillSummary] = []
    seen: set[str] = set()
    for name in must_include:
        spec = by_name.get(name)
        if spec is not None and name not in seen:
            chosen.append(spec)
            seen.add(name)
    for score, spec in scored:
        if len(chosen) >= max(1, size):
            break
        if spec.name in seen:
            continue
        if score <= 0.0 and chosen:
            # Nothing else matches at all — a shortlist of zeros invites a
            # confident wrong answer; the "none" option must stay plausible.
            break
        chosen.append(spec)
        seen.add(spec.name)
    return chosen


# --------------------------------------------------------------------------- #
# Arguments — the half Laya cannot do                                          #
# --------------------------------------------------------------------------- #


def _after(text: str, pattern: str) -> str:
    m = re.search(pattern, text)
    return m.group(1).strip(" ,.!?") if m else ""


def _capture(original: str, pattern: str, group: int = 1) -> str:
    """Match against what the user actually said, so casing survives.

    Values the user dictates are typed or searched as they said them
    ("Bob@Example.com", "Zed") — the normalized string is only for matching.
    """
    m = re.search(pattern, original, re.IGNORECASE)
    return m.group(group).strip(" ,.!?") if m else ""


def extract_args(skill: str, transcript: str) -> dict[str, Any] | None:
    """Pull the free-text arguments a skill needs out of the request.

    Returns `None` — not `{}` — when a needed argument cannot be read, so the
    caller can prefer a plan it can actually carry out over a guess. The
    phrasings mirror the deterministic rule layer exactly; that is deliberate:
    the two layers must agree on what "open X" means before Laya judges it.
    """
    t = normalize_request(transcript)
    original = str(transcript)
    if skill in _NO_ARG_SKILLS:
        return {}

    if skill in ("system.open_app", "system.quit_app"):
        verb = _OPEN_VERBS if skill == "system.open_app" else _QUIT_VERBS
        m = re.search(rf"(?:{verb})\s+(?:the\s+)?(?:app\s+)?(.+?)(?:\s+and|$)", t)
        if not m:
            return None
        target = m.group(1).strip(" ,.!?")
        if not target:
            return None
        # A domain is a website, not an installed application, and a popular
        # site name is not an installed app either (see browser.open_url).
        if _URLISH.search(target) or target in POPULAR_SITES:
            return None
        return {"app": title_case(target)}

    if skill == "system.set_volume":
        level = volume_from_text(t)
        return {"level": level} if level is not None else None

    if skill == "system.remember":
        fact = _capture(original, r"(?:remember|note) that (.+)")
        return {"fact": fact} if fact else None

    if skill == "browser.open_url":
        m = re.search(rf"(?:open|go to|visit|browse)\s+(?:the\s+)?(?:site\s+)?({_URLISH.pattern})", t)
        if m:
            return {"url": m.group(1).strip(" ,.!?").lower()}
        for site in POPULAR_SITES:
            if re.search(rf"\b{site}\b", t):
                return {"url": site}
        return None

    if skill == "browser.search":
        query = _capture(original, r"\b(?:search|look ?up|google|find)\b"
                                   r"(?:\s+(?:the\s+)?(?:web|online|internet))?"
                                   r"(?:\s+for)?\s+(.+)")
        query = re.sub(r"(?:\s+(?:on|in))?\s+(?:the\s+)?(?:web|online|internet)\s*$",
                       "", query, flags=re.IGNORECASE).strip()
        return {"query": query} if query else None

    if skill == "browser.focus_tab":
        title = _capture(original, r"(?:switch to|go to|focus|bring up)\s+(?:the\s+)?(.+?)\s+tab")
        return {"title": title} if title else None

    if skill == "clipboard.set_text":
        text = (_capture(original, r'copy\s+"([^"]+)"')
                or _capture(original, r"(?:copy|put)\s+(.+?)(?:\s+to\s+the\s+clipboard"
                                      r"|\s+into\s+the\s+clipboard|$)"))
        return {"text": text.strip()} if text.strip() else None

    if skill == "ax.click":
        target = _capture(original, r"(?:click|press|tap)\s+(?:on\s+)?(?:the\s+)?(.+)")
        return {"target": target} if target else None

    if skill == "ax.type_into":
        # Matched against the *original* text: a dictated value keeps its own
        # casing ("Bob@Example.com" must not become lowercase on the way in).
        m = re.search(r"type\s+(.+?)\s+into\s+(?:the\s+)?(.+)", original, re.IGNORECASE)
        if not m:
            return None
        value, target = m.group(1).strip(" ,.!?"), m.group(2).strip(" ,.!?")
        return {"text": value, "target": target} if value and target else None

    if skill == "ax.dictate":
        m = re.search(r"(?:type|dictate|say)\s*:?\s+(.+)$", original.strip(), re.IGNORECASE)
        value = m.group(1).strip(" ,.!?") if m else ""
        return {"text": value} if value else None

    if skill == "ax.fill_form":
        if re.search(r"\b(?:fill|complete)\b.*\b(?:form|fields)\b", t):
            return {"raw": original.strip()}
        return None

    if skill == "clipboard.get_text":
        return {}
    return None


def volume_from_text(text: str) -> int | None:
    """Digits, or the words people use instead of them."""
    m = re.search(r"(?:volume|sound|it|level)?\s*(?:to|at)?\s*(\d{1,3})\s*(?:percent|%)?", text)
    if m:
        return max(0, min(100, int(m.group(1))))
    if re.search(r"\b(?:max|maximum|full|all the way up|blast)\b", text):
        return 100
    if re.search(r"\b(?:half|50)\b", text):
        return 50
    if re.search(r"\b(?:low|quiet|soft|down|lower|quieter)\b", text):
        return 25
    if re.search(r"\b(?:high|loud|up|higher|louder)\b", text):
        return 80
    if re.search(r"\b(?:mute|silent|silence)\b", text):
        return 0
    return None


def volume_criteria() -> list[str]:
    """The scale Laya scores when the user says "turn it down a bit"."""
    return [str(n) for n in range(0, 101, 10)]


def reply_for(skill: str, args: dict[str, Any]) -> str:
    """One short spoken sentence for an action Laya chose.

    Grounded skills (click/type/read/fill) return "" on purpose: their own
    result message — computed from the live UI tree — is the honest reply.
    """
    table = {
        "system.open_app": lambda a: f"Opening {a.get('app', 'it')}.",
        "system.quit_app": lambda a: f"Quitting {a.get('app', 'it')}.",
        "system.set_volume": lambda a: f"Volume set to {a.get('level', 50)} percent.",
        "system.mute": lambda a: "Muted.",
        "system.brightness_up": lambda a: "Brightening the display.",
        "system.brightness_down": lambda a: "Dimming the display.",
        "system.toggle_dnd": lambda a: "Toggling Do Not Disturb.",
        "system.lock_screen": lambda a: "Locking your screen.",
        "system.sleep": lambda a: "Putting your Mac to sleep.",
        "system.empty_trash": lambda a: "This permanently deletes everything in the Trash — go ahead?",
        "system.start_recording": lambda a: "Starting a screen recording.",
        "system.remember": lambda a: "Noted — I'll remember that.",
        "browser.open_url": lambda a: f"Opening {a.get('url', 'that')}.",
        "browser.search": lambda a: f"Searching for {a.get('query', 'that')}.",
        "browser.list_tabs": lambda a: "Reading your open tabs.",
        "browser.focus_tab": lambda a: f"Switching to {a.get('title', 'that tab')}.",
        "clipboard.get_text": lambda a: "Reading your clipboard.",
        "clipboard.set_text": lambda a: "Copied to the clipboard.",
    }
    builder = table.get(skill)
    return builder(args) if builder else ""


# --------------------------------------------------------------------------- #
# The router                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class RouteResult:
    """What Laya decided, and everything needed to explain or debug it."""

    skill: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reply: str = ""
    risk: str = "safe"
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    considered: list[str] = field(default_factory=list)
    backend: str = ""
    source: str = ""              # "real" | "fallback"
    ms: float = 0.0
    error: str = ""
    reason: str = ""              # why the routing ended where it did

    @property
    def routed(self) -> bool:
        return bool(self.skill)

    def as_dict(self) -> dict[str, Any]:
        return {"skill": self.skill, "args": self.args, "confidence": round(self.confidence, 4),
                "backend": self.backend, "source": self.source, "ms": round(self.ms, 1),
                "considered": self.considered, "reason": self.reason, "error": self.error}


class LayaRouter:
    """Ask Laya which skill a request means; it answers with a probability."""

    def __init__(self, backend, specs: Sequence[SkillSummary], *,
                 shortlist_size: int = SHORTLIST_SIZE,
                 min_confidence: float = 0.30) -> None:
        self.backend = backend
        self.specs = coerce_specs(specs)
        self.by_name = {spec.name: spec for spec in self.specs}
        self.shortlist_size = max(2, int(shortlist_size))
        self.min_confidence = float(min_confidence)

    def route(self, transcript: str, *, must_include: Iterable[str] = ()) -> RouteResult:
        started = time.perf_counter()
        candidates = shortlist(transcript, self.specs, must_include=must_include,
                               size=self.shortlist_size)
        options = {spec.name: spec.option_text() for spec in candidates}
        options[NONE] = NONE_TEXT
        try:
            choice = self.backend.choose({"request": transcript}, ROUTE_INSTRUCTIONS, options)
        except Exception as exc:                      # a backend bug is not a session bug
            from .log import describe_exception

            reason = describe_exception(exc)
            log.error("route: the decision backend raised — %s", reason)
            return RouteResult(considered=list(options), backend="error", reason=reason,
                               error=reason, ms=(time.perf_counter() - started) * 1000.0)
        result = RouteResult(confidence=choice.confidence, probabilities=choice.probabilities,
                             considered=list(options), backend=choice.backend,
                             source=choice.source, error=choice.error)

        pick = choice.choice if choice.choice in options else ""
        if pick in ("", NONE, None):
            result.reason = (f"no skill chosen (confidence {choice.confidence:.2f} over "
                             f"{len(options)} options)")
            result.ms = (time.perf_counter() - started) * 1000.0
            log.info("route: %r → no skill (%s p=%.2f, backend=%s)", transcript[:60],
                     pick or "empty", choice.confidence, choice.backend)
            return result
        if choice.confidence < self.min_confidence:
            result.reason = (f"{pick} below the routing threshold "
                             f"({choice.confidence:.2f} < {self.min_confidence:.2f})")
            result.ms = (time.perf_counter() - started) * 1000.0
            log.info("route: %r → %s rejected: %s", transcript[:60], pick, result.reason)
            return result

        spec = self.by_name.get(pick)
        args = extract_args(pick, transcript)
        if args is None and pick == "system.set_volume":
            args = self._volume_by_score(transcript, result)
        if args is None:
            result.reason = f"{pick} chosen (p={choice.confidence:.2f}) but its arguments are not in the request"
            result.ms = (time.perf_counter() - started) * 1000.0
            log.info("route: %r → %s (p=%.2f) but no arguments could be read",
                     transcript[:60], pick, choice.confidence)
            return result

        result.skill = pick
        result.args = args
        result.reply = reply_for(pick, args)
        result.risk = spec.risk if spec else "safe"
        result.reason = f"laya chose {pick} (p={choice.confidence:.2f}, {choice.backend})"
        result.ms = (time.perf_counter() - started) * 1000.0
        return result

    def _volume_by_score(self, transcript: str, result: RouteResult) -> dict[str, Any] | None:
        """`score`: "turn it down a bit" → a number, from the ordered scale."""
        score = self.backend.score({"request": transcript},
                                   "What output volume level does the user want?",
                                   volume_criteria())
        result.error = result.error or score.error
        if score.source == "real" or score.error == "":
            index = round(score.index)
            return {"level": max(0, min(100, index * 10))}
        return None
