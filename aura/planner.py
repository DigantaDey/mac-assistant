"""The planner: turns a transcript into a small, inspectable Plan.

Three layers, in the order a request meets them:

1. **Rules (`MockPlanner`)** — a deterministic keyword table that answers the
   everyday commands ("open spotify", "set volume to 30") in microseconds and
   extracts their free-text arguments exactly.
2. **Laya (`LayaPlanner`)** — everything the rules cannot route goes to the
   decision model, which *chooses* the skill from a shortlist with a
   calibrated probability (one forward pass). Laya never writes text, so it
   cannot invent an argument: the value still comes from the rules' extractor,
   and a skill whose arguments cannot be read is refused rather than guessed.
3. **The optional local LLM (`OpenAICompatPlanner` / `HybridPlanner`)** — a
   fully local OpenAI-compatible endpoint (Ollama, mlx_lm, llama.cpp, LM
   Studio) for the open-ended remainder, still available behind
   `planner.engine = "openai_compat"`.

None of them decides *safety*: that is the Laya gate's job (`aura/laya.py`).
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import intent as intent_mod
from .laya import HeuristicBackend, LayaBackend, laya_available
from .log import describe_exception, get_logger

log = get_logger("planner")

try:  # optional — the deterministic basic mode works without it
    import httpx
except Exception:  # pragma: no cover - depends on host
    httpx = None  # type: ignore[assignment]

RISKS = ("safe", "confirm")

# Product latency contract. The orchestrator owns the five-second end-to-end
# deadline; the model gets a smaller slice so fallback and UI delivery still
# fit inside it. This cap deliberately wins over stale user configuration.
MAX_MODEL_SECONDS = 4.0

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
    # "laya" | "rules" | "llm" — which layer produced it. The everyday commands
    # are answered by the rules in microseconds; what they cannot route is
    # Laya's call (see LayaPlanner.plan).
    source: str = "laya"
    # False when the layer handled only *part* of the request — a chain where
    # one step didn't route. A partial plan is worth handing to Laya.
    complete: bool = True
    # Time spent inside Laya (routing + choice/score questions) in ms — the
    # number that tells you whether the fast path is actually fast.
    model_ms: float = 0.0
    # "rules" | "laya" | "laya+rules" | "llm" | "none"
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
# Prompt construction                                                          #
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are Aura, a private voice assistant running entirely on the user's Mac.
Turn the user's request into JSON. Never invent skills. Never output prose outside JSON.

Schema:
{{
  "reply": "<one short spoken sentence — what you are doing or asking>",
  "actions": [
    {{"skill": "<name from the catalog>", "args": {{}}, "risk": "safe"|"confirm", "why": "<short reason>"}}
  ]
}}

Rules:
- At most {max_actions} actions, executed in order.
- risk "confirm" for anything destructive, irreversible, or that sends/creates/shares something on the user's behalf.
- If the request is ambiguous, reply with a short question and no actions.
- If nothing matches the catalog, say so honestly and suggest the closest skill.
- Skill catalog:
{catalog}
"""


def render_system_prompt(catalog_prompt: str, max_actions: int) -> str:
    return SYSTEM_PROMPT.format(catalog=catalog_prompt, max_actions=max_actions)


# --------------------------------------------------------------------------- #
# Response parsing — small local models need a tolerant reader                 #
# --------------------------------------------------------------------------- #


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first balanced JSON object out of arbitrary model output."""
    text = text.strip()
    # Fast path
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Strip markdown fences if present
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # Brace matching scan
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def parse_plan(raw: dict[str, Any] | None, latency_ms: int = 0) -> Plan:
    """Validate loose model JSON into a strict Plan; drop anything malformed."""
    if raw is None:
        return Plan(reply="I couldn't work out a plan for that — could you rephrase?",
                    latency_ms=latency_ms)
    reply = str(raw.get("reply", "")).strip()[:500]
    actions: list[Action] = []
    for item in raw.get("actions", []) or []:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill", "")).strip()
        if not skill:
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        risk = str(item.get("risk", "safe")).lower()
        actions.append(Action(skill=skill, args=args,
                              risk=risk if risk in RISKS else "safe",
                              why=str(item.get("why", "")).strip()[:200]))
    if not reply and not actions:
        reply = "I'm not sure how to help with that yet."
    return Plan(reply=reply, actions=actions, latency_ms=latency_ms)


# --------------------------------------------------------------------------- #
# OpenAI-compatible planner (Ollama / mlx_lm / llama-server / LM Studio)      #
# --------------------------------------------------------------------------- #


class OpenAICompatPlanner(Planner):
    def __init__(self, cfg, catalog_prompt: str) -> None:
        if httpx is None:
            raise RuntimeError("httpx is not installed — pip install -e '.[mac]'")
        self.base_url = cfg.planner.base_url.rstrip("/")
        self.model = cfg.planner.model
        self.api_key = cfg.planner.api_key
        self.temperature = cfg.planner.temperature
        self.timeout = max(0.1, min(float(cfg.planner.timeout_seconds), MAX_MODEL_SECONDS))
        self.max_actions = cfg.planner.max_actions
        self.system = render_system_prompt(catalog_prompt, cfg.planner.max_actions)
        # Few-shot anchors keep tiny models on-format.
        self.system += (
            '\nExample — user: "mute the sound and open safari" → '
            '{"reply":"Muting and opening Safari.","actions":['
            '{"skill":"system.mute","args":{},"risk":"safe","why":"asked to mute"},'
            '{"skill":"system.open_app","args":{"app":"Safari"},"risk":"safe","why":"asked to open Safari"}]}'
        )
        # Persistent client — reused across all requests (TCP connection keep-alive).
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init a long-lived async client with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=3.0),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        return self._client

    async def close(self) -> None:
        """Release the persistent client's connection pool."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def plan(self, transcript: str, context: dict[str, Any]) -> Plan:
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system}]
        # Only the 2 most recent turns — reduces prompt tokens and speeds inference.
        for turn in context.get("recent_turns", [])[-2:]:
            messages.append({"role": turn["role"], "content": str(turn["content"])[:200]})
        messages.append({"role": "user", "content": transcript})

        started = time.monotonic()
        client = self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": 400,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except httpx.ConnectError:
            # Connection refused → close the stale client so next call retries.
            await self.close()
            raise
        latency = int((time.monotonic() - started) * 1000)
        return parse_plan(extract_json_object(content), latency)


# --------------------------------------------------------------------------- #
# Hybrid planner — the real brain, with an honest basic mode                   #
# --------------------------------------------------------------------------- #


class HybridPlanner(Planner):
    """LLM-first planning with a deterministic fallback for core intents.

    The local LLM (Ollama, mlx_lm, …) is the brain; when it is unreachable —
    not installed, model not pulled, or the machine just booted — Aura does
    not go silent. It degrades to the built-in basic intents ("open X",
    "set volume to N", "search for Y", …) and flags the plan `degraded` so
    the UI can say so. A fast /models probe with a short-TTL cache decides
    which path to take, and failed requests re-probe on the next attempt.
    """

    def __init__(self, cfg, catalog_prompt: str) -> None:
        self.cfg = cfg
        self.llm = OpenAICompatPlanner(cfg, catalog_prompt)
        self.basic = MockPlanner(catalog_prompt, cfg.planner.max_actions)
        self._online: bool = False
        self._last_probe = 0.0
        self._last_error = ""
        # Persistent sync client for probes — avoids TCP teardown/setup per check.
        self._probe_client: httpx.Client | None = None

    def _get_probe_client(self) -> httpx.Client:
        if self._probe_client is None or self._probe_client.is_closed:
            self._probe_client = httpx.Client(
                timeout=httpx.Timeout(1.5, connect=1.0),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            )
        return self._probe_client

    # -- availability ------------------------------------------------------ #

    def probe(self) -> bool:
        """Is the local LLM endpoint answering? Cached: ≤1 check / 15 s."""
        now = time.monotonic()
        if self._online and now - self._last_probe < 15.0:
            return True
        if now - self._last_probe < 3.0:
            return self._online
        self._last_probe = now
        try:
            client = self._get_probe_client()
            resp = client.get(f"{self.llm.base_url}/models")
            self._online = resp.status_code == 200
            if self._online:
                self._last_error = ""
        except Exception as exc:
            self._online = False
            self._last_error = str(exc).splitlines()[0][:160]
            # Close stale probe client so next probe gets a fresh connection.
            try:
                if self._probe_client and not self._probe_client.is_closed:
                    self._probe_client.close()
                    self._probe_client = None
            except Exception:
                pass
        return self._online

    @property
    def status(self) -> dict[str, Any]:
        """For /api/state: engine, live status, and the last error, if any."""
        return {"engine": "openai_compat", "online": self._online,
                "model": self.llm.model, "base_url": self.llm.base_url,
                "last_error": self._last_error}

    def warmup(self) -> bool:
        """Load the local model before the user asks for anything.

        A cold Ollama spends tens of seconds loading qwen3:4b on the first
        request — which is precisely the "it's still thinking" the user feels.
        One token of work, in the background at engine start, moves that cost
        off the first command. Best-effort: returns False and stays silent
        when there is no model server to warm.
        """
        if not self.probe():
            return False
        try:
            client = self._get_probe_client()
            resp = client.post(
                f"{self.llm.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.llm.api_key}"},
                json={"model": self.llm.model,
                      "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 1, "temperature": 0.0},
                # Warm-up must never monopolise Ollama while a real command
                # waits behind it. A cold load may continue server-side, but
                # Aura's own worker is released within the same model budget.
                timeout=httpx.Timeout(MAX_MODEL_SECONDS, connect=1.0),
            )
            return resp.status_code == 200
        except Exception as exc:
            self._online = False
            self._last_error = str(exc).splitlines()[0][:160]
            return False

    async def plan(self, transcript: str, context: dict[str, Any]) -> Plan:
        """Reflex first: rules, then the model, then rules again.

        The deterministic layer answers the everyday commands ("open youtube",
        "set volume to 30") in microseconds, and it never needs the model
        server, RAM or a warm-up. Sending those to a 4B model was pure added
        latency — the "it's still thinking" feeling — so the model is now
        consulted only for what the rules genuinely cannot route, or for a
        request they only half-handled. The Laya gate judges the result either
        way, so routing faster does not mean acting less carefully.
        """
        import asyncio as _asyncio

        plan = await self.basic.plan(transcript, context)
        if plan.actions and plan.complete:
            plan.degraded = False
            return plan

        # Not (fully) routable by rules — this is what a local model is for.
        # The probe is only paid when we actually need the model.
        online = await _asyncio.get_running_loop().run_in_executor(None, self.probe)
        if online:
            try:
                plan = await self.llm.plan(transcript, context)
                plan.degraded = False
                plan.source = "llm"
                return plan
            except Exception as exc:
                self._online = False
                self._last_error = str(exc).splitlines()[0][:160]
                # fall through — the user still gets an answer
        plan = await self.basic.plan(transcript, context)
        plan.degraded = True
        return plan


# --------------------------------------------------------------------------- #
# Deterministic mock planner — demo profile, basic mode & tests                #
# --------------------------------------------------------------------------- #


class MockPlanner(Planner):
    """The deterministic layer: reflexes, no model, no network, ~0.01 ms.

    This is what runs when the local LLM is unreachable ("basic mode") *and*
    the fast path that answers the everyday commands without one. It is a
    keyword table, so it only understands phrasings it has been taught — the
    LLM covers the rest. A rule layer fails by *refusing*, which is why the
    Laya gate still judges every action it produces.
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
        # (The real local LLM does this natively; the mock mirrors it.)
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

    #: How much of the request's latency budget Laya routing may take.
    ROUTE_BUDGET_MS = 2500

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

    * ``mock``   — demo profile & tests
    * ``laya``   — LayaPlanner: rules parse, the decision model routes
                   (default; needs no model server at all)
    * ``auto``   — Laya when the package is installed, otherwise the local
                   OpenAI-compatible model if httpx is available, otherwise the
                   rules alone
    * ``openai_compat`` — the LLM-first HybridPlanner (kept for people who run
                   Ollama/mlx_lm and want generated text)
    """
    engine = str(getattr(cfg.planner, "engine", "laya") or "laya").lower()
    if engine == "mock":
        log.info("planner: mock (demo profile), %d skills", len(specs or []))
        return MockPlanner(catalog_prompt, cfg.planner.max_actions)

    if engine in ("laya", "auto"):
        if engine == "laya":
            if laya_backend is None:
                log.error("planner: engine=laya but no Laya backend was provided — "
                          "the rules answer and routing uses the offline scorer")
                laya_backend = HeuristicBackend()
            return LayaPlanner(cfg, catalog_prompt, laya_backend, specs)
        if laya_backend is not None and laya_available():
            return LayaPlanner(cfg, catalog_prompt, laya_backend, specs)

    if engine in ("auto", "openai_compat") and httpx is not None:
        try:
            planner = HybridPlanner(cfg, catalog_prompt)
            log.info("planner: hybrid local LLM at %s (model=%s)",
                     cfg.planner.base_url, cfg.planner.model)
            return planner
        except Exception as exc:
            log.error("planner: could not build the local LLM planner — %s",
                      describe_exception(exc))
    log.warning("planner: falling back to the deterministic rule layer")
    return MockPlanner(catalog_prompt, cfg.planner.max_actions)
