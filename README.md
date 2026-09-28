# Aura

**Your machine, understood.** A private, offline, voice-first assistant for
macOS — a **native menu-bar app** with the whole engine running on your Mac.

There is no cloud account, no telemetry, and **no browser**: Aura is an
`LSUIElement` app built with SwiftUI and AppKit that talks to a local Python
engine over `127.0.0.1`. Say it, type it, or tap the orb — Aura listens,
understands, plans, and asks before anything risky.

> **TL;DR** — Say “*Hey Aura, open YouTube*.” Or type it. Or click the ◉ in
> the menu bar. Everything happens on this Mac.

---

## Install (macOS 13+, Apple Silicon or Intel)

```bash
git clone <this repo> mac-assistant && cd mac-assistant
./scripts/install.sh
```

One command, and it is idempotent — re-run it any time to update:

1. installs the native bits with Homebrew (PortAudio, whisper.cpp, Ollama);
2. pulls the reasoning model (`qwen3:4b`, one-time, cached);
3. downloads the speech model (`ggml-base.en`, ≈150 MB, one-time);
4. copies the engine to `~/Library/Application Support/Aura/engine` and builds
   a Python environment for it;
5. builds **Aura.app** and installs it into **/Applications**;
6. opens it — and Aura walks you through the two macOS permissions, one click
   each.

Then:

- click **◉** in the menu bar, or press **⌥Space** anywhere, and say
  “*open YouTube*” — or just type it;
- say “*empty the trash*” — Aura **stops and asks** before anything risky;
- open **Settings** any time: permissions, wake phrase, voice, model, safety,
  data and the log — all in the app, no terminal, no browser.

### Just want the engine? (any OS)

The engine is a plain Python package with a token-guarded JSON API; it runs
head-less anywhere (that is how the test suite and CI use it):

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m aura serve      # → prints the token path and the port
.venv/bin/python -m aura doctor     # honest capability matrix
```

There is deliberately **no web UI** — the product is the app.

---

## What makes Aura different

| | Cloud assistants | Aura |
|---|---|---|
| **Where it runs** | their data centres | your Mac, offline |
| **The app** | a web page in a wrapper | native SwiftUI + AppKit menu-bar app |
| **Voice** | mic always streaming to a server | on-device wake word + push-to-talk + typing |
| **Memory** | their profiles | a local SQLite file you can open and delete |
| **Safety** | “trust us” | a *decision gate* scores every action for **match** and **destructiveness**; risky things **ask you** |
| **When the model is down** | nothing happens | Aura falls back to built-in skills and says so (“running on basics”) — never silent failure |
| **What it can touch** | whatever the vendor allows | only what macOS grants *you*: Microphone, Accessibility, Automation — each revocable |

### The action pipeline

```
you ── voice / type ──►  STT (whisper.cpp or faster-whisper, on-device)
                              │ transcript
                              ▼
                        Planner — the local OpenAI-compatible model server
                          (Ollama `qwen3:4b` by default; mlx_lm, llama.cpp
                          and LM Studio all work; deterministic built-in
                          planner when nothing answers)
                              │ plan: which skills, with what arguments
                              ▼
                        The gate (aura/laya.py)
                          scores: match (did I understand?) &
                                  destructive (is this risky?)
                              │
              ┌───────────────┴───────────────┐
        safe ▼                               risky ▼
     just do it                    ASK: Run it / Cancel  (native card)
```

Every outcome — done, you confirmed, you cancelled, you corrected — is
recorded locally. That log is what the optional Laya fine-tune loop consumes
(`scripts/nightly_laya.py`), so Aura's judgement improves from *your* choices.

---

## What Aura can do today

24 skills, all auditable in `aura/skills/`:

- **Forms** — “*fill this form: name John, email me at smith dot com*”. Aura
  reads the live accessibility tree, maps your words onto the right fields,
  and types each value. It will **never** press submit for you: finishing a
  form is confirm-gated. Also “*what fields does this form have?*” and plain
  dictation (“*type 123 Main Street*”).
- **System** — open and quit apps, volume, mute, brightness, Do Not Disturb,
  lock, sleep, screenshots, empty the trash (asks first), launch a screen
  recording (asks first).
- **Automation** — Apple events and Accessibility: click a named control,
  type into a named field, read the frontmost window, list and focus browser
  tabs.
- **Browser & clipboard** — open URLs, search, read/copy the clipboard.
- **Memory** — “*remember that my editor is Zed*”, and preferences you
  correct in Activity.
- **Understanding** — contextual, compound requests: “*quiet the house*” →
  DND + volume.

The catalog is data: a `SkillSpec` (name, description, argument hints,
examples, default risk) plus one `execute()` — it then appears to the planner,
the app's Skills list, and the safety manifest automatically.

---

## The app

```
◉ menu-bar orb        left-click  → the panel
   │                  right-click → menu (settings, setup, activity,
   │                                restart engine, login item, log, quit)
   │
   ├─ panel (SwiftUI popover, 400×620)
   │    state orb · live transcript · reply
   │    confirmation card: the plan, risks, Run / Cancel (⌘↩ / Esc)
   │    composer + suggestions · Activity · My data
   │
   ├─ Settings window — 8 sections, driven by the engine's own state
   │    General · Voice · Understanding · Safety · Permissions ·
   │    Wake Phrase · Activity · About
   │
   ├─ First run — a native welcome window: what stays on this Mac, the two
   │    permissions, and three commands to try
   │
   └─ ⌥Space anywhere — a real global shortcut (Carbon, no permission needed)
```

The icon tells you the state at a glance: **white** ready · **blue** working ·
**orange** waiting for your OK · **red** needs attention.

---

## Installer layout

```
aura/              the engine — orchestrator, planner, gate, STT/TTS, wake
                   word, VAD, skills, memory, permissions, local API
macos/             the app — Swift package (no Xcode project needed)
  Sources/AuraCore/     Foundation-only: API client, SSE, supervisor, paths
  Sources/AuraMenuBar/  the product: AppKit + SwiftUI views, hotkey, windows
  Tests/AuraCoreTests/  unit tests that run on every push
scripts/           install.sh · make_app.sh · make_icon.py · nightly_laya.py
                   check_swift_syntax.py · ci_swift_report.sh
assets/            app icon (generated by scripts/make_icon.py)
tests/             200+ tests — engine, planner fallbacks, permission honesty,
                   access control, skill catalog, ship-quality regressions
ARCHITECTURE.md    how the pieces talk
ROADMAP.md         where the product is going, what shipped when
docs/RESEARCH.md   the model choices, with the papers behind them
```

---

## Security model (read this bit)

Aura's engine listens on loopback — but *loopback is not a security boundary*:
any process on your Mac can reach a local port, and so can any web page you
have open. So the engine behaves like a serious local service:

- **every request needs a token** (`X-Aura-Token`), stored 0600 in
  `~/Library/Application Support/Aura/token` and handed to the engine by the
  app through `AURA_TOKEN`;
- requests carrying a browser **`Origin`** are refused, and so is any
  **`Host`** that isn't loopback — that kills cross-site request forgery and
  DNS rebinding without needing CORS at all (no CORS headers are ever sent);
- the socket binds `127.0.0.1` only; widening it needs an explicit
  `AURA_ALLOW_REMOTE=1` and prints a warning;
- there is no HTML, no static files and no browser UI in the engine at all —
  unknown paths answer JSON `404`.

Permissions are still macOS's: Aura asks through TCC (Microphone,
Accessibility, per-app Automation) and reports the *real* answer, never a
guess. Revoke anything in System Settings and Aura's Setup screen will say so
on the next refresh.

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q      # engine: 200+ tests
.venv/bin/ruff check .                    # lint, must be clean
.venv/bin/python scripts/check_swift_syntax.py   # Swift syntax gate (any OS)

./scripts/make_app.sh                     # build the app (macos/build/Aura.app)
./scripts/make_app.sh --install           # …and put it in /Applications
cd macos && swift test                    # AuraCore unit tests (needs a Mac)
```

CI runs the engine tests on Linux **and** builds the app, runs the Swift
tests, and validates the bundle on a macOS runner — including a check that
WebKit never creeps back in. The one part that can't be compiled off-Mac is
covered there on every push.

---

## Honest limitations

- The **default** brain is a local model server (Ollama `qwen3:4b`, ≈2.5 GB).
  Without it Aura still works — she says “running on basics” and uses the
  built-in planner, which understands a fixed set of phrasings.
- The **decision gate** ships with a deterministic, explainable backend. The
  optional `laya` package (non-autoregressive, ~33 ms on MLX) plugs into the
  same seam when you install it.
- Wake-word recall depends on your mic and your phrase — it's a tuning knob,
  not a science. Train your own phrase in Settings ▸ Wake Phrase and Aura
  learns how *you* say it (recordings never leave the Mac).
- Ad-hoc signed by default: perfect for your own Mac, not for distribution yet
  (see ROADMAP for Developer ID + notarisation).

---

Aura is built to a simple bar: **every claim in this README should be true,
and the tests should be able to prove it.** If you find one that isn't, that's
a bug — please open an issue.
