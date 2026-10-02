"""The planner: turns a transcript into a small, inspectable Plan.

Aura's brain is Laya — there is no LLM anywhere in the product. Two layers,
in the order a request meets them:

1. **Rules (`MockPlanner`)** — a deterministic reflex table that answers the
   everyday commands ("open spotify", "set volume to 30") in microseconds and
   extracts their free-text arguments exactly.
2. **Laya (`LayaPlanner`)** — everything the rules cannot route goes to the
   decision model, which *chooses* the skill from a shortlist with a
   calibrated probability (one forward pass). Laya never writes text, so it
   cannot invent an argument: the value comes from the rules' extractor, and
   a skill whose arguments cannot be read is refused rather than guessed.

Neither layer decides *safety*: that is the Laya gate's job (`aura/laya.py`).
"""

from __future__ import annotations

import asyncio
import functools
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import intent as intent_mod
from .laya import HeuristicBackend, LayaBackend
from .log import describe_exception, get_logger

log = get_logger("planner")

RISKS = ("safe", "confirm")

# Names people mean as websites, not locally installed applications. Keep the
# URL resolution itself in the browser skill; the planner only picks the right
# execution surface. In particular, `tell application "YouTube"` can make
# macOS wait for an app-selection/Automation dialog when no such app exists.
POPULAR_SITES = {
    "youtube", "github", "gmail", "google", "maps", "calendar", "whatsapp", "reddit",
}


@dataclass
class Action:
    skill: str
    args: dict[str, Any] = field(default_factory=dict)
    risk: str = "safe"          # may be corrected by skills registry + safety gate
    why: str = ""               # one short line, shown in the UI timeline

    def as_dict(self) -> dict[str, Any]:
        return {"skill": self.skill, "args": self.args, "risk": self.risk, "why": self.why}


@dataclass
class Plan:
    reply: str
    actions: list[Action] = field(default_factory=list)
    latency_ms: int = 0
    # True when the plan came from the deterministic layer because the real
    # brain was unreachable — the UI shows a subtle "basic mode" note.
    degraded: bool = False
    # "laya" | "rules" — which layer produced it. The everyday commands are
    # answered by the rules in microseconds; what they cannot route is Laya's
    # call (see LayaPlanner.plan). There is no LLM layer — by design.
    source: str = "laya"
    # False when the layer handled only *part* of the request — a chain where
    # one step didn't route. A partial plan is worth handing to Laya.
    complete: bool = True
    # Time spent inside Laya (routing + choice/score questions) in ms — the
    # number that tells you whether the fast path is actually fast.
    model_ms: float = 0.0
    # "rules" | "laya" | "laya+rules" | "none"
    routed_by: str = ""
    # Engineer-readable reason when degraded/failed. Logged, shown in the UI's
    # log, and (for failures) short enough to put in front of the user.
    diagnostic: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"reply": self.reply, "actions": [a.as_dict() for a in self.actions],
                "latency_ms": self.latency_ms, "degraded": self.degraded,
                "source": self.source, "complete": self.complete,
                "model_ms": self.model_ms, "routed_by": self.routed_by,
                "diagnostic": self.diagnostic}


class Planner:
    async def plan(self, transcript: str, context: dict[str, Any]) -> Plan:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Deterministic mock planner — demo profile, basic mode & tests                #
# --------------------------------------------------------------------------- #


class MockPlanner(Planner):
    """The deterministic layer: reflexes, no model, no network, ~0.01 ms.

    This is the reflex layer: the fast path that answers the everyday
    commands with no model at all. It is a keyword table, so it only
    understands phrasings it has been taught — Laya routes everything it
    cannot. A rule layer fails by *refusing*, which is why the Laya gate
    still judges every action it produces.
    """

    # What we say when nothing matches. Hoisted so the chain splitter can tell
    # "handled the whole request" from "dropped a step".
    NO_SKILL = ("I don't have a skill for that yet — try “open Spotify”, "
                "“set volume to 30”, “search for airport lounges”, "
                "or “remember that …”.")

    def __init__(self, catalog_prompt: str, max_actions: int = 3) -> None:
        self.max_actions = max_actions

    @staticmethod
    def _title_case(name: str) -> str:
        return intent_mod.title_case(name)

    async def plan(self, transcript: str, context: dict[str, Any]) -> Plan:
        started = time.monotonic()
        # Chain support: "open spotify and set volume to 30" → two actions.
        # A dictated form fill is atomic — its "and submit" belongs to the
        # fill, not to a second step, so it stays one part.
        if re.search(r"\b(?:fill|complete)\b.*\b(?:form|fields)\b", transcript, re.IGNORECASE):
            parts = [transcript.strip()]
        else:
            parts = [p for p in re.split(r"\s+and then\s+|\s+and\s+", transcript.strip(), maxsplit=2)
                     if p.strip()] or [transcript.strip()]
        actions: list[Action] = []
        replies: list[str] = []
        complete = True
        for part in parts[: self.max_actions]:
            sub = await self._plan_one(part)
            actions.extend(sub.actions)
            if sub.reply:
                replies.append(sub.reply)
            complete = complete and sub.complete
        actions = actions[: self.max_actions]
        latency = max(1, int((time.monotonic() - started) * 1000))
        return Plan(reply=" ".join(replies), actions=actions, latency_ms=latency,
                    source="rules", complete=complete)

    async def _plan_one(self, transcript: str) -> Plan:
        t = transcript.lower().strip()
        # Everyday politeness: "can you open spotify please" is the same
        # request as "open spotify". A rule layer that can't see through it
        # refuses a command it perfectly well understands.
        t = re.sub(r"^(?:hey aura[,!.]?\s+)?(?:can|could|would|will) you (?:please )?",
                   "", t)
        t = re.sub(r"^(?:please|pls|kindly)\s+", "", t)
        t = re.sub(r"[,!.]?\s*(?:please|thanks|thank you)\s*[.!]?$", "", t)
        t = t.strip(" ,.!?")
        actions: list[Action] = []
        reply = ""
        complete = True

        m = re.search(r"(?:remember|note) that (.+)", t)
        if m:
            actions.append(Action("system.remember", {"fact": m.group(1)}, "safe",
                                  "asked to remember something"))
            reply = "Noted — I'll remember that."
        elif "what can you do" in t or "help" == t:
            reply = ("I can open and quit apps, control volume and brightness, search the "
                     "web, manage tabs and clipboard, remember facts, and more — ask away.")
        elif m := re.search(r"open (?:the )?(?:app )?(.+?)(?: and|$)", t):
            app = self._title_case(m.group(1))
            target = app.lower()
            if "." in app or target in POPULAR_SITES:
                # “Open YouTube” means the website for the overwhelming
                # majority of Mac users. Route it to `open <url>` instead of
                # AppleScript, which can block while looking for a nonexistent
                # app or waiting on an Automation dialog.
                actions.append(Action("browser.open_url", {"url": target},
                                      "safe", "opening site"))
                reply = f"Opening {app}."
            else:
                actions.append(Action("system.open_app", {"app": app}, "safe", "asked to open it"))
                reply = f"Opening {app}."
        elif re.search(r"\b(?:close|shut|dismiss|hide)\b[^.]*\btabs?\b"
                       r"|\btabs?\b[^.]*\b(?:close|shut|dismiss)\b", t) or \
                re.search(r"\b(?:next|previous|prev|last|switch(?: to)?|go to|back to)\b"
                          r"[^.]*\btabs?\b", t):
            # Aura has no close/switch-tab skill yet. Refusing honestly beats
            # the old behaviour, where "close this tab" reached the quit-app
            # rule and became quit_app(app="This Tab") — a confident, wrong
            # action against an application that does not exist.
            reply = ("I can't close or switch tabs yet — I can list them, focus one "
                     "by name, or open a new one.")
            complete = False
        elif m := re.search(r"(?:open|launch|fire up|pull up|bring up|switch to|go to) "
                            r"(?:the )?(?:app )?(.+?)(?: and|$)", t):
            app = self._title_case(m.group(1))
            target = app.lower()
            if "." in app or target in POPULAR_SITES:
                actions.append(Action("browser.open_url", {"url": target},
                                      "safe", "opening site"))
                reply = f"Opening {app}."
            else:
                actions.append(Action("system.open_app", {"app": app}, "safe", "asked to open it"))
                reply = f"Opening {app}."
        elif m := re.search(r"(?:quit|close|kill|force ?quit|terminate|shut down) (.+)", t):
            app = self._title_case(m.group(1))
            actions.append(Action("system.quit_app", {"app": app}, "confirm", "closing an app can lose work"))
            reply = f"Quitting {app}."
        elif m := re.search(r"(?:set )?volume (?:to )?(\d+)", t):
            vol = max(0, min(100, int(m.group(1))))
            actions.append(Action("system.set_volume", {"level": vol}, "safe", "asked to change volume"))
            reply = f"Volume set to {vol} percent."
        elif "mute" in t:
            actions.append(Action("system.mute", {}, "safe", "asked to mute"))
            reply = "Muted."
        elif re.search(r"\b(?:brightness|screen|display)\b", t) and \
                re.search(r"\b(?:up|down|higher|lower|brighter|dimmer)\b", t):
            # A nudge, not a level — the skills are nudges too.
            dim = bool(re.search(r"\b(?:down|lower|dimmer)\b", t))
            actions.append(Action("system.brightness_down" if dim else "system.brightness_up",
                                  {}, "safe", "asked to nudge the display"))
            reply = "Dimming the display." if dim else "Brightening the display."
        elif "do not disturb" in t or "focus mode" in t:
            actions.append(Action("system.toggle_dnd", {}, "safe", "toggling focus"))
            reply = "Toggling Do Not Disturb."
        elif "empty the trash" in t or "empty trash" in t:
            actions.append(Action("system.empty_trash", {}, "confirm", "permanently deletes files"))
            reply = "This permanently deletes everything in the Trash — go ahead?"
        elif "lock" in t and "screen" in t:
            actions.append(Action("system.lock_screen", {}, "safe", "asked to lock"))
            reply = "Locking your screen."
        elif "sleep" in t:
            actions.append(Action("system.sleep", {}, "confirm", "puts the Mac to sleep"))
            reply = "Putting your Mac to sleep."
        elif m := re.search(r"click (?:on )?(?:the )?(.+)", t):
            target = m.group(1).strip(" .!?")
            actions.append(Action("ax.click", {"target": target}, "safe", "click by label"))
            reply = ""  # the grounded skill message ("Pressed … 95% sure") is the reply
        elif ("fill" in t or "complete" in t) and ("form" in t or "fields" in t or "this out" in t):
            # Dictated form fill — the skill parses the raw dictation against
            # the live form. "…and press submit" becomes one extra,
            # confirm-gated action; the fill itself stays frictionless.
            actions.append(Action("ax.fill_form", {"raw": transcript.strip()}, "safe",
                                  "user dictated the form's values"))
            m_sub = re.search(
                r"(?:and|then)\s+(?:press|click|hit|tap|select)?\s*(?:the )?"
                r"(submit|send|save|continue|sign ?up|register|complete|check ?out)\b", t)
            if m_sub:
                actions.append(Action("ax.click", {"target": m_sub.group(1)}, "confirm",
                                      "presses the form's ending button"))
            reply = ""  # the skill's grounded message ("Filled … with …") is the reply
        elif m := re.search(r"type (.+?) into (?:the )?(.+)", t):
            # Values keep their original casing — passwords aren't lowercase.
            m2 = re.search(r"type (.+?) into (?:the )?(.+)", transcript, re.IGNORECASE)
            text = (m2.group(1) if m2 else m.group(1)).strip()
            actions.append(Action("ax.type_into",
                                  {"text": text, "target": m.group(2).strip(" .!?")},
                                  "safe", "type into a named field"))
            reply = ""
        elif m := re.search(r"^(?:please )?(?:type|dictate|say)\s*:?\s+(.+)$", t):
            # Plain dictation into whatever field is focused — the fast path.
            m2 = re.search(r"^(?:please )?(?:type|dictate|say)\s*:?\s+(.+)$",
                           transcript, re.IGNORECASE)
            text = (m2.group(1) if m2 else m.group(1)).strip(" .!?")
            actions.append(Action("ax.dictate", {"text": text}, "safe",
                                  "typed into the focused field"))
            reply = ""
        elif ("form" in t or "fields" in t) and re.search(r"\b(what|list|which|show)\b", t):
            actions.append(Action("ax.read_form", {}, "safe", "listing the form's fields"))
            reply = ""
        elif "on my screen" in t or "read my screen" in t or "what do you see" in t:
            actions.append(Action("ax.read_screen", {}, "safe", "reading visible controls"))
            reply = ""
        elif m := re.search(r"\b(?:search|look ?up|google)\b"
                            r"(?:\s+(?:the\s+)?(?:web|online|internet))?"
                            r"(?:\s+for)?\s+(.+)", t):
            query = m.group(1).strip("?.!")
            # "look up airport lounges online" is a search for the lounges.
            query = re.sub(r"(?:\s+(?:on|in))?\s+(?:the\s+)?"
                           r"(?:web|online|internet)\s*$", "", query).strip()
            if query:
                actions.append(Action("browser.search", {"query": query}, "safe", "web search"))
                reply = f"Searching for {query}."
        elif m := re.search(r"(?:open|go to) (\S+\.(?:com|org|net|io|dev|ai)[^ ]*)", t):
            url = m.group(1)
            actions.append(Action("browser.open_url", {"url": f"https://{url}"}, "safe", "opening site"))
            reply = f"Opening {url}."
        elif "tabs" in t and "list" in t:
            actions.append(Action("browser.list_tabs", {}, "safe", "reading open tabs"))
            reply = "Reading your open tabs."
        elif "clipboard" in t and ("read" in t or "what" in t):
            actions.append(Action("clipboard.get_text", {}, "safe", "reading the clipboard"))
            reply = "Reading your clipboard."
        elif "copy" in t and "clipboard" in t:
            m2 = re.search(r'copy "([^"]+)"', t) or re.search(r"copy (.+)", t)
            text = m2.group(1).strip() if m2 else ""
            actions.append(Action("clipboard.set_text", {"text": text}, "safe", "asked to copy"))
            reply = "Copied to the clipboard."
        elif re.search(r"\b(?:record|recording|screen ?record(?:ing)?)\b", t) and \
                not re.search(r"\b(?:stop|end|finish|cancel|pause)\b", t):
            actions.append(Action("system.start_recording", {}, "confirm", "captures the screen"))
            reply = "Starting a screen recording."
        else:
            reply = self.NO_SKILL
            complete = False

        return Plan(reply=reply, actions=actions, complete=complete)


# --------------------------------------------------------------------------- #
# The Laya planner — the decision model IS the brain                          #
# --------------------------------------------------------------------------- #


class LayaPlanner(Planner):
    """Rules parse, Laya decides.

    Every request first meets the deterministic rule table, because a reflex
    must not pay for a model: "open spotify" is a complete plan in
    microseconds, arguments included. Laya — one `choice` question over a
    shortlist of plausible skills, answered with a calibrated probability in a
    single forward pass — takes everything the rules cannot route, which is
    where a keyword table is at its worst:

        "quiet the house"      → system.toggle_dnd
        "turn it down a bit"   → system.set_volume (a `score` question)
        "put the machine to bed" → system.sleep

    The division of labour is not a compromise, it is a capability boundary:
    Laya cannot generate text, so it cannot invent an argument. Arguments come
    from `aura.intent.extract_args`, and a skill whose arguments cannot be read
    from the request is **refused**, never guessed.

    When Laya is unavailable (package missing, weights not downloaded, or a
    call failed) the rules still answer, the plan is flagged `degraded`, and
    `diagnostic` carries the reason — visible in the log and in the UI. Silence
    was the old failure mode; it is not one any more.
    """

    #: How much of the request's latency budget Laya routing may take. Warm,
    #: routing is tens of milliseconds; this bound is for a cold checkpoint
    #: load landing inside a session. When it trips, the rules' answer (or an
    #: honest "no skill") is returned — the session never hangs on the model.
    ROUTE_BUDGET_MS = 10_000

    def __init__(self, cfg, catalog_prompt: str, backend: LayaBackend | None = None,
                 specs: list[Any] | None = None) -> None:
        self.cfg = cfg
        self.rules = MockPlanner(catalog_prompt, cfg.planner.max_actions)
        self.backend = backend if backend is not None else HeuristicBackend()
        self.specs = (intent_mod.coerce_specs(specs) if specs
                      else intent_mod.specs_from_catalog(catalog_prompt))
        self.router = intent_mod.LayaRouter(
            self.backend, self.specs,
            min_confidence=float(getattr(cfg.laya, "route_threshold", 0.30) or 0.30),
        )
        log.info("planner: laya (%d skills, backend=%s, min_confidence=%.2f)",
                 len(self.specs), type(self.backend).__name__, self.router.min_confidence)

    # The orchestrator's maintenance loop reads this for /api/state.
    @property
    def status(self) -> dict[str, Any]:
        try:
            return self.backend.status()
        except Exception as exc:            # pragma: no cover - introspection only
            return {"backend": type(self.backend).__name__, "error": describe_exception(exc)}

    def warmup(self) -> bool:
        """Load the model in the background (never blocks a session)."""
        try:
            return bool(self.backend.warmup())
        except Exception as exc:
            log.error("planner: laya warm-up failed — %s", describe_exception(exc))
            return False

    async def plan(self, transcript: str, context: dict[str, Any]) -> Plan:
        started = time.monotonic()
        rule_plan = await self.rules.plan(transcript, context)
        rule_plan.routed_by = "rules"

        # 1 — chit-chat and refusals the rules answered in words. There is no
        #     action to route, and a model call would add nothing.
        if not rule_plan.actions and rule_plan.complete and rule_plan.reply:
            return self._finish(rule_plan, started, source="rules")

        # 2 — a complete deterministic plan is already the right answer.
        #     (Laya still judges the *action* — see the safety gate — but
        #     routing it again would only add latency to a reflex.)
        if rule_plan.actions and rule_plan.complete:
            log.debug("plan: rules handled %r → %s", transcript[:60],
                      [a.skill for a in rule_plan.actions])
            return self._finish(rule_plan, started, source="rules")

        # 3 — everything else is Laya's call.
        must = [action.skill for action in rule_plan.actions]
        result = await self._route(transcript, must)
        model_ms = max(0.0, round(result.ms, 1))

        if result.error:
            # The fallback answered. Say so, with the reason, and keep the
            # rules' honest reply if there is one.
            rule_plan.degraded = True
            rule_plan.diagnostic = result.error
            log.warning("plan: laya routing fell back to the offline scorer for %r — %s",
                        transcript[:60], result.error)

        if result.skill and result.skill not in must:
            action = Action(result.skill, result.args, result.risk or "safe",
                            f"chosen by {result.backend} (p={result.confidence:.2f})")
            reply = result.reply or rule_plan.reply
            actions = ([*rule_plan.actions, action] if rule_plan.actions else [action])
            plan = Plan(reply=reply, actions=actions[: self.rules.max_actions],
                        complete=True, degraded=rule_plan.degraded,
                        source="laya", model_ms=model_ms,
                        routed_by="laya+rules" if rule_plan.actions else "laya",
                        diagnostic=rule_plan.diagnostic)
            log.info("plan: laya routed %r → %s%s (p=%.2f, %.0fms, backend=%s)",
                     transcript[:60], result.skill, result.args, result.confidence,
                     result.ms, result.backend)
            return self._finish(plan, started, source="laya")

        if result.skill and result.skill in must:
            # Laya agrees with the rules — the partial plan stands as it was.
            log.info("plan: laya confirmed the rules' choice %s (p=%.2f, %.0fms)",
                     result.skill, result.confidence, result.ms)

        if not result.skill:
            rule_plan.diagnostic = rule_plan.diagnostic or result.reason
            rule_plan.degraded = rule_plan.degraded or bool(result.error)
            log.info("plan: nothing routed %r — %s", transcript[:60], result.reason)

        return self._finish(rule_plan, started, source="rules", model_ms=model_ms)

    async def _route(self, transcript: str, must: list[str]) -> intent_mod.RouteResult:
        """Run the routing call off the event loop, under a hard deadline.

        A model call is foreign code: it may download a checkpoint, or wedge.
        Off-loop + timeout keeps the orb breathing and the SSE stream live even
        when the model misbehaves, and the watchdog still owns the session.
        """
        loop = asyncio.get_running_loop()
        call = functools.partial(self.router.route, transcript, must_include=must)
        # `shield` matters: since 3.11 `wait_for` waits for the cancellation it
        # requested, and a *running* executor job cannot be cancelled — without
        # it a wedged model would hold the session for its full duration
        # instead of the budget. The abandoned call finishes on its own thread.
        running = loop.run_in_executor(None, call)
        running.add_done_callback(
            lambda fut: fut.cancelled() or fut.exception() is None)
        try:
            return await asyncio.wait_for(asyncio.shield(running),
                                          timeout=self.ROUTE_BUDGET_MS / 1000.0)
        except TimeoutError:
            reason = f"laya routing did not answer within {self.ROUTE_BUDGET_MS} ms"
            log.error("plan: %s", reason)
            return intent_mod.RouteResult(error=reason, reason=reason,
                                          backend=type(self.backend).__name__)
        except Exception as exc:      # a router bug must not cost the user an answer
            reason = describe_exception(exc)
            log.error("plan: laya routing raised — %s", reason)
            return intent_mod.RouteResult(error=reason, reason=reason,
                                          backend=type(self.backend).__name__)

    def _finish(self, plan: Plan, started: float, source: str,
                model_ms: float = 0.0) -> Plan:
        plan.source = source
        if not plan.routed_by:
            plan.routed_by = source
        if model_ms:
            plan.model_ms = model_ms
        plan.latency_ms = max(1, int((time.monotonic() - started) * 1000))
        return plan


def build_planner(cfg, catalog_prompt: str, specs: list[Any] | None = None,
                  laya_backend: LayaBackend | None = None):
    """Choose the planner for this machine — and say which one, and why.

    * ``laya``        — LayaPlanner: rules parse the everyday commands, the
                        decision model routes the rest. The default, and the
                        only brain the product ships — no model server, no LLM.
    * ``rules``/``mock`` — the deterministic rule layer alone (demo profile,
                        tests, or a deliberate minimal install).

    Any legacy value (`auto`, `openai_compat`, …) maps to `laya`: the LLM
    planners were removed when Laya became the brain — Aura never phones out
    to a model server any more.
    """
    engine = str(getattr(cfg.planner, "engine", "laya") or "laya").lower()
    if engine in ("mock", "rules"):
        log.info("planner: rules only (engine=%s), %d skills", engine, len(specs or []))
        return MockPlanner(catalog_prompt, cfg.planner.max_actions)

    if engine != "laya":
        log.warning("planner: unknown engine %r — Aura's brain is Laya; using it", engine)
    if laya_backend is None:
        log.error("planner: engine=laya but no Laya backend was provided — "
                  "the rules answer and routing uses the offline scorer")
        laya_backend = HeuristicBackend()
    return LayaPlanner(cfg, catalog_prompt, laya_backend, specs)
