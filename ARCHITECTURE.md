# Architecture

One rule shaped everything here: **the product must be boring to trust and
fast to feel.** That yields a strict pipeline with one narrow brain, one
paranoid gate, and dumb-but-perfect executors.

```
┌────────────────────────────── macOS process ──────────────────────────────┐
│                                                                            │
│  MicStream (16k int16) ──► WakeEngine ──► EnergyVAD ──► STT                │
│      ▲ sounddevice        openWakeWord       energy     whisper.cpp/       │
│      │  + SilentMic       + phrase gate    + hangover   faster-whisper     │
│      │                                                                    │
│      │        ┌──────────────── Orchestrator (async state machine) ─────┐ │
│      │        │ armed → capturing → transcribing → planning → gating    │ │
│      │        │   ▲                    │                                │ │
│      │        │   │      confirm/cancel/correct                         │ │
│      │        │   │                    ▼                                │ │
│      │        │ responding ◄── executing ◄── proposing (timeout 45 s)   │ │
│      │        └────────────────────────┬────────────────────────────────┘ │
│      │                                 │                                  │
│  TTS  │        Planner ────────► SafetyGate ────────► SkillRegistry        │
│ `say` │  OpenAI-compat local    blocklist          24 declared skills      │
│ /Piper│  LLM (Ollama/mlx_lm/    + skill manifest   AppleScript · AX · `open`│
│       │  llama.cpp/LM Studio)   + Laya gate        · pbcopy/pbpaste        │
│       │                                 │                                  │
│  EventBus ──► SSE ──► UI (127.0.0.1)    ▼                                  │
│                ▲              Memory (SQLite: events · preferences ·       │
│                └────────────── laya_examples) ──► nightly fine-tune        │
└────────────────────────────────────────────────────────────────────────────┘
```

## Layers

### 1. Voice front-end (`audio.py`, `wakeword.py`, `vad.py`, `stt.py`, `tts.py`)
- Only the **wake model stays resident** (~tens of MB). STT/LLM load lazily on
  wake and unload after `session.idle_unload_seconds` — that's the ~200 MB idle
  promise.
- Wake supports pretrained openWakeWord models and user-trained custom-phrase
  in-app training (the Wake Phrase panel). A configurable **spoken phrase
  gate** adds a second factor: in always-on mode the transcript must begin
  with the user's phrase, killing false accepts.
- STT is a one-method interface with three backends; whisper.cpp is preferred
  on Apple Silicon (CoreML/ANE encoder). TTS defaults to `say` (zero extra RAM,
  offline, instant); Piper/Kokoro are drop-in upgrades.

### 2. The planner (`planner.py`)
A deliberately *narrow* LLM contract:

```json
{"reply": "one short spoken sentence",
 "actions": [{"skill": "system.open_app", "args": {"app": "Spotify"},
              "risk": "safe", "why": "asked to open it"}]}
```

- Runs against any local OpenAI-compatible server. 4B-class quantized models
  hold this format reliably with a tight prompt + one few-shot anchor.
- Parsing is defensive (fence-stripping, brace-matching, schema coercion):
  a malformed model output degrades to a clarifying question, never a crash.
- **`HybridPlanner` (v0.5):** the planner *is* the LLM client plus a
  built-in-skill fallback behind one interface. A cheap, throttled
  `GET /api/health` probe decides; offline ⇒ the plan comes from the skill
  catalog ("open youtube" still opens YouTube) and the reply carries
  `degraded: true` so the UI can say "running on basics". A planner crash
  mid-session is caught by the orchestrator's session guard and answered
  the same way — a slow Mac or a cold-starting Ollama can never make a
  command vanish.
- The planner never decides *safety*. It proposes; the gate disposes.

### 3. The Laya gate (`laya.py`) — the signature layer
For every proposed action, two typed questions answered with calibrated
probabilities in a single forward pass:

- *Does this action match what the user asked for?* → `match`
- *Is this destructive, irreversible, or shared beyond the device?* → `destructive`

**Policy:** `match ≥ threshold` and `destructive < threshold` ⇒ run.
Anything else ⇒ propose and wait for a human. Below-threshold confidence is
treated as *risk*, never as permission.

The backend is swappable: the real `laya` package (MLX/CoreML) when installed;
a deterministic, explainable `HeuristicBackend` otherwise (demo profile, CI,
first runs). Same answers, same interface, one-file swap.

### 4. The learning loop (`memory.py`, `laya.py`, `scripts/nightly_laya.py`)
- **Tier 1 — memory, always on:** every session lands in SQLite; durable
  preferences ("my editor is Zed") are recalled into planner context; history
  is FTS-searchable.
- **Tier 2 — Laya fine-tune, nightly:** the gate logs every decision *with its
  outcome* (auto-run / confirmed / corrected / cancelled) as supervision.
  `scripts/nightly_laya.py` exports JSONL, fine-tunes the 421M-param Laya
  adapter on-device (minutes), and hot-swaps it. Corrections get 2× weight.
- **Tier 3 — deep personalization (roadmap):** occasional QLoRA of the planner
  model from accumulated preferences + procedures learned by demonstration.

### 5. Skills (`skills/`)
*A v0.3 addition — the accessibility layer:*
- **`ax.py`** exposes the frontmost app's real UI tree (pyobjc over
  AXUIElement) or a deterministic `MockAXTree` — one interface, press /
  focus / insert.
- **`picker.py`** chooses the element the user meant: options are chunked
  (≤16), chunks are coarse-ranked, only the top chunks' options are
  fine-scored — Laya typed questions when the real backend runs, a
  deterministic token-overlap heuristic otherwise. Below threshold ⇒
  "did you mean …", never a guess.
- **`skills/accessibility.py`** turns that into voice: "click the sign in
  button", "type aura into the search field", "what's on my screen".
- **`skills/forms.py`** fills whole forms from dictation:
  **`formfill.py`** scans the live tree for fields/buttons, maps spoken
  values onto labels (grounded — an offline LLM pass only refines when the
  heuristic matched nothing), and types with original casing kept.
  `ax.fill_form` (safe), `ax.read_form` (reads the fields), `ax.dictate`
  (~100 ms typing into the focused field). Pressing a submit button is
  always a separate confirm-gated action.

A skill = `SkillSpec` (catalog + risk declaration) + one async `execute()`.
macOS work goes through **`MacBridge`** (osascript/CLI); **`DryRunBridge`**
logs the exact same calls without executing — that's how the demo profile and
tests run the *real orchestrator* on any machine, and how you can preview a
plan before ever granting permissions.

Adding a skill is ~20 lines + one `register()` call; it automatically appears
in the planner catalog, the UI, and the safety manifest.

### 6. Server & UI (`server.py`, `ui/`)
- Stdlib `ThreadingHTTPServer` + SSE: **zero web dependencies**, loopback-only
  by default. POSTs hop to the orchestrator's loop via
  `run_coroutine_threadsafe`; events fan out through the `EventBus`.
- **Thread-correct by construction (v0.5):** the HTTP handlers are *server*
  threads; everything they touch on the orchestrator is scheduled — futures
  resolved with `call_soon_threadsafe`, bus delivery re-dispatched onto the
  owning loop, config changes applied *on* the loop (where wake/STT rebuilds
  are safe). The v0.5 release fixed a real deadlock this rule catches
  (confirm-posted-from-a-browser-thread hung the session forever) and
  regression-tests it over real HTTP.
- The endpoint surface: chat (`/api/input|confirm|cancel|correct`), state
  (`/api/state|health|history|skills|metrics`), the consent layer
  (`/api/permissions|request`), the live settings layer
  (`/api/config` — validated, persisted, hot-applied), setup
  (`/api/setup|setup/step`), and wake/training control (`/api/wake*`).
- The UI is hand-rolled HTML/CSS/JS — no framework, no build step, no fonts to
  download (system SF stack). One SSE stream drives everything: orb states,
  live transcripts, plan proposals, confirm/cancel (⌘↩ / esc), the activity
  timeline with 👍/👎 correction, skills grid, settings. A compact layout
  (sidebar collapses to an icon rail under 620 px) makes it first-class inside
  the 480 pt menu-bar popover.

### 7. Permissions (`permissions.py`, Setup wizard)
Aura treats TCC as *the* consent system, not an obstacle. Every check is
honest and live: Accessibility via `AXIsProcessTrusted()`, microphone via
whether Aura's own audio bridge opened, automation via a harmless AppleEvent
actually sent (the consent dialog is the feature), whisper/LLM readiness via
real binary/endpoint probes. The Setup wizard renders these as cards with
deep links (`x-apple.systempreferences:…`) into the exact Privacy panes,
a progress bar, and a Check-again loop. Nothing is faked, anywhere.

### 8. The native shell (`macos/`)
An AppKit executable built with `swift build` (no Xcode project):
- **First run is native (v0.5):** a *Welcome to Aura* `NSWindow` hosts the
  Setup wizard in a `WKWebView` (the browser never appears), and the app
  itself asks macOS for Microphone (its bundle carries
  `NSMicrophoneUsageDescription`) and shows the Accessibility prompt —
  proactively, seconds after launch, once.
- `NSStatusItem` whose icon mirrors orchestrator state (polled from
  `/api/health` every 2 s: ready / busy / needs-OK / offline, with
  offline→ready reload), a full right-click menu (open, wake, setup,
  browser, restart engine, choose folder, login, log, quit), an
  `NSPopover`+`WKWebView` panel behind a native "Waking Aura…" readiness
  gate, and a global ⌥Space hotkey via Carbon (`RegisterEventHotKey` — no
  permission needed).
- The Python process babysitter discovers the engine (UserDefaults →
  `Application Support/Aura/engine`, the installer's home → file picker),
  spawns `.venv/bin/python -m aura serve` with `AURA_ENGINE_PATH` /
  `AURA_DATA_DIR` and a GUI-safe PATH, rotates logs to
  `~/Library/Logs/Aura.log`, restarts after crashes; `SMAppService`
  login item. `scripts/make_app.sh` assembles and ad-hoc-signs `Aura.app`
  (generated icon from `scripts/make_icon.py` — no binary hand-maintenance).

## Configuration
`config.default.toml` → user `config.toml` → `runtime.toml` (written by the
app) → `AURA_*` env vars. The `auto` profile resolves to **mac** on macOS
and **demo** elsewhere — but off-Mac the demo *base* is applied first and
overridable, so developers can point the brain at their Ollama without
touching the profile. `LIVE_FIELDS` marks the safe subset the UI may change
at runtime (`POST /api/config` validates type + membership, persists to
`runtime.toml`, hot-applies on the loop); restart-required fields are
refused with a friendly message.

## Testing
144 tests cover the JSON parser, mock planner routing, the **hybrid
planner's fallbacks** (dead endpoint ⇒ degraded plan that still acts), the
**session guard** (planner crash ⇒ session still answers), wake fallback
honesty, safety verdicts, the Laya heuristic + example buffer,
memory/FTS/preferences, the **thread-correct confirm flow over real HTTP**,
live config round-trips and refusals, and **full orchestration over real
HTTP** (typed session, confirmation flow, cancel flow, busy-state hints,
feedback recording). The dry-run bridge makes the entire product testable on
Linux CI — the same code path a Mac runs.
