# Aura

**Your machine, understood.** A private, offline, voice-first assistant for
macOS — built around Apple's **on-device Foundation Models** (the same API
family as Apple Intelligence), with a **local Llama 3.2 1B** safety brain
(**Laya**) that double-checks every action, and a hard rule: *confirm
anything destructive.*

Aura runs entirely on your Mac. No cloud, no telemetry, no account — and it
wears the face of a real Mac app: a menu-bar orb that listens, asks, and
acts.

> **TL;DR** — Say “*Hey Aura, open YouTube*.” Type it. Click the icon and
> ask. Aura listens, understands, plans, and — when something is risky —
> asks you first. All of it, on your machine.

---

## Quick start (macOS 15+, Apple Silicon)

```bash
git clone <this repo> mac-assistant && cd mac-assistant
./scripts/install.sh
```

That's the whole install. One command:

1. brews the native bits (PortAudio, whisper.cpp), starts **Ollama** and
   pulls the **qwen3:4b** reasoning brain (one-time, cached after);
2. downloads the speech model (≈150 MB) and Aura's engine (Python + venv,
   zero cloud);
3. builds **Aura.app**, installs it into **/Applications**, and opens it;
4. Aura walks you through permissions — Microphone, Accessibility,
   Automation — with one click each.

Then:

- click the **◉** in the menu bar, or press **⌥Space** anywhere, and say
  “*open YouTube*” — or just type it;
- say “*empty the trash*” — Aura **stops and asks** before anything risky;
- open **Settings** any time to change the mic, the brain, wake phrase, or
  see exactly what Aura can and can't do.

### Already like it in a browser? (any OS)

```bash
python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m aura serve
# → open http://127.0.0.1:7331  (auto profile: everything works, nothing is faked)
```

The same web panel, same engine, same code — the menu-bar app is a thin
native shell around it.

---

## What makes Aura different

| | Cloud assistants | Aura |
|---|---|---|
| **Where it runs** | their data centers | your Mac — always offline |
| **Voice** | mic off → nothing | always-on wake word + push-to-talk + typing |
| **Memory** | their profiles | local preference notes + a growing lesson log it fine-tunes on |
| **Safety** | “trust us” | a local Llama 3.2 1B (Laya) scores *match* and *destructiveness* of every action; anything risky **asks you** |
| **The “brain”** | one monolith | **on-device Apple Foundation Models** for understanding + **local qwen3 (Ollama)** for planning + **GPT-4o mini** as an *optional, explicit* fallback you can turn on |
| **When the model is down** | nothing | falls back to built-in skills + a visible “running on basics” badge — never silent failure |
| **Privacy** | your data is their product | audio, transcripts, and memory never leave the machine (unless *you* opt in to the cloud brain) |

### The action pipeline

```
you ── voice / type ──►  STT (whisper.cpp, on-device)
                              │ transcript
                              ▼
                        Planner — on-device Apple Foundation Models
                          (local qwen3 via Ollama; GPT-4o mini only if
                          you switch the engine in Settings → Brain)
                              │ plan: which skills, with what args
                              ▼
                        Laya — local Llama 3.2 1B (llama.cpp)
                          scores: match (did I understand?) &
                          destructive (is this risky?)
                              │
              ┌───────────────┴───────────────┐
        safe ▼                               risky ▼
     just do it                         ASK the user (Run / Change / Stop)
```

Every outcome (done / you confirmed / you corrected it / you stopped it)
is recorded to Aura's **lesson log** — the training set for fine-tuning
Laya on your machine later (ROADMAP).

---

## What Aura can do today

- **System** — open apps, find files, copy / set the clipboard, mute,
  DND, sleep, screenshots, volume, trash (asks first), quit apps (asks)
- **Automation** — System Events (launch/quit/set frontmost), Accessibility
  UI control, browser tabs — a growing, tested skill catalog
- **Understanding** — on-device: “*quiet the house*” → DND + volume;
  “*find my Q3 deck*” → search; compound requests plan into several steps
- **Ongoing** — preferences Aura learns from your corrections
  (“I prefer Spotify for music”) and remembers in Settings

The skill catalog is data, not code — add a skill by adding JSON
(`aura/skills/*.json`); tests keep them honest.

## The product, end to end

```
install  ──►  permissions  ──►  first command  ──►  daily
1 cmd      mic / access /     “open youtube”     ◉ menu bar · ⌥Space
script     automation (one    answers + acts     Settings: permissions,
           click each)                         brain, wake, data, log
```

Everything you can change, you can change **later**, from
**Settings** — permissions (re-ask any time), brain engine, wake mode,
voice on/off, confirmation mode, and Aura's data folder.

## Doctor

Aura checks itself and tells you, in plain English, what's ready and what
needs you:

```bash
.venv/bin/python -m aura doctor     # models, permissions, ports, config
.venv/bin/python -m aura --version
```

## Layout

```
aura/            the engine — server (HTTP + SSE), orchestrator, planner,
                 Laya, STT (whisper.cpp / Apple), TTS, wake word, VAD,
                 skills, memory, permissions
  server.py      the local API (127.0.0.1:7331): chat, state, setup steps,
                 permission requests, live config, wake/training control
ui/              the web panel — single page, no build step
macos/           the native shell — AppKit menu-bar app + WKWebView
                 (swift build, no Xcode project)
scripts/         install.sh (one command) · make_app.sh · make_icon.py
                 nightly_laya.py
tests/           144+ tests — engine, planner fallbacks, permission honesty,
                 skill catalog, ship-quality regressions
ARCHITECTURE.md  how the pieces talk (engine, Laya, hybrid planner, shell)
ROADMAP.md       where the product is going, what shipped when
docs/RESEARCH.md the model choices, with the papers behind them
```

## Honest limitations (read before judging)

- **macOS 15 / Apple Silicon** for the on-device foundation model brain;
  on other machines Aura runs the same code with the local-qwen or
  built-in-skill brains and says so.
- The Foundation Models call is **on-device but private** — prompts go to
  the system framework, not to OpenAI. The GPT-4o mini fallback is the only
  network path in the project, and it's off by default.
- Voice quality depends on your mic; wake-word recall is a tuning knob
  (`config: [wake] sensitivity`), not a science.
- Destructive actions ask. *Everything else just happens* — that's the
  point, and the reason Laya scores match, not just risk.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check
.venv/bin/python -m aura serve      # browser preview at http://127.0.0.1:7331
```

See `ARCHITECTURE.md` for how the pieces talk, `ROADMAP.md` for what ships
next, and `docs/RESEARCH.md` for the model choices (with the papers behind
them).

---

Aura is a research-grade product: the bar is “Apple could ship this.”
The roadmap holds it there.
