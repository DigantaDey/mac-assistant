"""The planner: turns a transcript into a small, inspectable Plan.

Aura deliberately keeps the LLM's job *narrow* — understand, pick skills,
fill arguments, write one short reply. It never chooses *how* to do things
safely; that is the Laya gate's job. This is what lets a 4B model run the
show locally without feeling dumb or acting dangerous.

Any OpenAI-compatible endpoint works and every recommended one is local:
Ollama, mlx_lm.server, llama-server, LM Studio. The demo profile uses a
deterministic MockPlanner so the full product flow can be exercised on any
machine — and so tests never flake on a model.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

try:  # optional — the deterministic basic mode works without it
    import httpx
except Exception:  # pragma: no cover - depends on host
    httpx = None  # type: ignore[assignment]

RISKS = ("safe", "confirm")


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
    # True when the plan came from the deterministic basic layer because the
    # local LLM was unreachable — the UI shows a subtle "basic mode" note.
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"reply": self.reply, "actions": [a.as_dict() for a in self.actions],
                "latency_ms": self.latency_ms, "degraded": self.degraded}


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
        self.timeout = cfg.planner.timeout_seconds
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
                timeout=httpx.Timeout(300.0, connect=3.0),
            )
            return resp.status_code == 200
        except Exception as exc:
            self._online = False
            self._last_error = str(exc).splitlines()[0][:160]
            return False

    async def plan(self, transcript: str, context: dict[str, Any]) -> Plan:
        # Run the synchronous probe in an executor so it doesn't block the
        # event loop while waiting for the HTTP health check.
        import asyncio as _asyncio
        online = await _asyncio.get_running_loop().run_in_executor(None, self.probe)
        if online:
            try:
                plan = await self.llm.plan(transcript, context)
                plan.degraded = False
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
    """Keyword table that mimics a well-tuned small model. Deterministic."""

    def __init__(self, catalog_prompt: str, max_actions: int = 3) -> None:
        self.max_actions = max_actions

    @staticmethod
    def _title_case(name: str) -> str:
        return " ".join(w.capitalize() for w in name.strip().split())

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
        for part in parts[: self.max_actions]:
            sub = await self._plan_one(part)
            actions.extend(sub.actions)
            if sub.reply:
                replies.append(sub.reply)
        actions = actions[: self.max_actions]
        latency = max(1, int((time.monotonic() - started) * 1000))
        return Plan(reply=" ".join(replies), actions=actions, latency_ms=latency)

    async def _plan_one(self, transcript: str) -> Plan:
        t = transcript.lower().strip()
        actions: list[Action] = []
        reply = ""

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
            if "." in app:  # a domain, not an app → browser skill
                actions.append(Action("browser.open_url", {"url": app}, "safe", "opening site"))
                reply = f"Opening {app}."
            else:
                actions.append(Action("system.open_app", {"app": app}, "safe", "asked to open it"))
                reply = f"Opening {app}."
        elif m := re.search(r"(?:quit|close) (.+)", t):
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
        elif "brightness up" in t:
            actions.append(Action("system.brightness_up", {}, "safe", "asked to brighten"))
            reply = "Brightening the display."
        elif "brightness down" in t:
            actions.append(Action("system.brightness_down", {}, "safe", "asked to dim"))
            reply = "Dimming the display."
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
        elif m := re.search(r"search(?: the web)?(?: for)? (.+)", t):
            query = m.group(1).strip("?.!")
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
        elif "recording" in t and ("start" in t or "begin" in t):
            actions.append(Action("system.start_recording", {}, "confirm", "captures the screen"))
            reply = "Starting a screen recording."
        else:
            reply = ("I don't have a skill for that yet — try “open Spotify”, “set volume "
                     "to 30”, “search for airport lounges”, or “remember that …”.")

        return Plan(reply=reply, actions=actions)


def build_planner(cfg, catalog_prompt: str):
    """Choose the planner for this machine.

    * mock          — demo profile & tests
    * auto / openai_compat — HybridPlanner (LLM + basic-mode fallback) when
      httpx is available; otherwise the basic layer alone, honestly labeled.
    """
    if cfg.planner.engine == "mock":
        return MockPlanner(catalog_prompt, cfg.planner.max_actions)
    if cfg.planner.engine in ("auto", "openai_compat") and httpx is not None:
        try:
            return HybridPlanner(cfg, catalog_prompt)
        except Exception:
            pass
    return MockPlanner(catalog_prompt, cfg.planner.max_actions)
