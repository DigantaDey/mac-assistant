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
│      │        │ responding ◄── executing ◄── proposing (timeout 20 s)   │ │
│      │        └────────────────────────┬────────────────────────────────┘ │
│      │                                 │                                  │
│  TTS  │        Planner ────────► SafetyGate ────────► SkillRegistry        │
│ `say` │  rules, then Laya       blocklist          24 declared skills      │
│ /Piper│  bundled checkpoint     + skill manifest   AppleScript · AX · `open`│
│       │                           + Laya gate      · pbcopy/pbpaste        │
│       │                                 │                                  │
│  EventBus ──► SSE ──► UI (127.0.0.1)    ▼                                  │
│                ▲              Memory (SQLite: events · preferences ·       │
│                └────────────── laya_examples) ──► nightly fine-tune        │
└────────────────────────────────────────────────────────────────────────────┘
```

## Layers

### 1. Voice front-end (`audio.py`, `wakeword.py`, `vad.py`, `stt.py`, `tts.py`)
- Only the **wake model stays resident** (~tens of MB). STT loads lazily on
  wake and unload after `session.idle_unload_seconds` — that's the ~200 MB idle
  promise.
- Wake supports pretrained openWakeWord models and user-trained custom-phrase
  in-app training (the Wake Phrase panel). A configurable **spoken phrase
  gate** adds a second factor: in always-on mode the transcript must begin
  with the user's phrase, killing false accepts — which is why the template
  threshold sits deliberately below the midpoint between the weakest positive
  and the strongest negative: a false accept costs one rejected
  transcription, a false reject makes the product read as dead.
- **Training and detection share one score space.** The trainer calibrates
  the threshold by sliding the *detector's own* windows (a trailing 1 s span
  every ~128 ms, plus one offset 384 ms behind it) across every training
  take, so the stored number is reachable by what the microphone stream
  actually produces. An earlier build embedded whole ~2 s VAD captures
  instead; their silence-diluted mean-pooled spectra scored ~0.999 against
  their own template while live windows measured ~0.985 — trained phrases
  never fired and nothing was logged. Templates carry a `calibration`
  version: v1 files still load, but the engine flags them and the panel
  recommends retraining. Detectors also publish a rolling **listening level**
  (`wake_level` beside `wake_threshold` in `/api/state`), which the Wake
  Phrase panel renders as a live meter — "did Aura hear me?" is answerable
  without a log dive.
- The gate only works because of the **pre-roll**: a detector can only fire
  once the phrase is behind it, so the orchestrator keeps ~2 s of audio behind
  the wake engine and seeds the VAD with it (`EnergyVAD.prime`). The phrase
  survives into the recording, the transcript really does begin with it, and a
  pause between phrase and command is bridged by a longer grace rather than
  ending the turn.
- STT is a one-method interface with three backends; whisper.cpp is preferred
  on Apple Silicon (CoreML/ANE encoder). TTS defaults to `say` (zero extra RAM,
  offline, instant); Piper/Kokoro are drop-in upgrades.

### 2. The planner (`planner.py`)
Three layers behind one contract — and the plan is always a *typed object*
(`Plan`), never free text:

```json
{"reply": "one short spoken sentence",
 "actions": [{"skill": "system.open_app", "args": {"app": "Spotify"},
              "risk": "safe", "why": "asked to open it"}],
 "source": "rules|laya|laya+rules|none", "complete": true,
 "model_ms": 12.4, "degraded": false, "diagnostic": ""}
```

- **Rules first (microseconds).** The deterministic table parses everyday
  commands — verbs, apps, URLs, volume, chains — with arguments, and returns a
  complete plan. A reflex must never wait for a model, so this layer is
  untouched by anything below it.
- **Laya second (`LayaPlanner`, the default, `planner.engine = "laya"`).**
  Whatever the rules cannot place goes to one `choice` question over a
  shortlist of the skill catalog (retrieval + an intent lexicon pick the
  options; the model picks the skill), plus a `score` question when a value
  lives on a scale (volume). A partial rule plan is *extended*, not replaced;
  a pick below `laya.route_threshold` means "no skill" and is never acted on.
- **There is no fourth layer.** Aura deliberately has no LLM planner — a
  local chat model's latency (seconds to minutes per request) is a worse
  trade than refusing and saying so. Legacy `planner.engine` values
  (`auto`, `openai_compat`) are mapped to Laya with a warning.
- **Arguments are never generated.** They come from deterministic extraction
  (`aura/intent.extract_args`) or a closed set offered to Laya. A skill whose
  argument cannot be read from the request is *refused*, not guessed — the one
  hallucination this design makes impossible.
- **Degradation is visible.** Every plan carries `source`, `routed_by`,
  `model_ms`, `degraded` and a human-readable `diagnostic`. A planner crash is
  caught by the session guard; the user gets the reason, the log gets the
  traceback.
- The planner never decides *safety*. It proposes; the gate disposes.

### 3. The Laya gate (`laya.py`) — the signature layer
For every proposed action, two typed questions answered with calibrated
probabilities in a single forward pass:

- *Does this action match what the user asked for?* → `match`
- *Is this destructive, irreversible, or shared beyond the device?* → `destructive`

**Policy:** `match ≥ threshold` and `destructive < threshold` ⇒ run.
Anything else ⇒ propose and wait for a human. Below-threshold confidence is
treated as *risk*, never as permission.

The backend is swappable: the real `laya` package when installed — both
questions are asked in **one `Router.predict()` call**, so a decision costs one
forward pass — and a deterministic, explainable `HeuristicBackend` otherwise
(demo profile, CI, first runs). `LayaGate` owns the real backend: it loads
weights in the background, bounds every call with `laya.call_budget_seconds`,
degrades for a cooldown after a timeout, and returns an answer *plus the
reason* instead of an exception. Every fallback is logged with its traceback
and carried on the `Decision` (`error`, `source`).

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
  values onto labels (heuristic only — nothing is guessed), and types with
  original casing kept.
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

### 6. The engine's local API (`server.py`, `localauth.py`)
- Stdlib `ThreadingHTTPServer` + SSE: **zero web dependencies**, bound to
  `127.0.0.1` only. POSTs hop to the orchestrator's loop via
  `run_coroutine_threadsafe`; events fan out through the `EventBus`.
- **It is not a website.** There is no HTML, no static file serving and no
  browser UI: `/` answers a JSON banner, unknown paths answer JSON `404`,
  unsupported methods answer JSON `405`. The product UI is the native app.
- **Access control (v0.6).** A loopback port is reachable by any process and
  by any web page you have open, so every request must carry
  `X-Aura-Token` — a 32-byte secret stored `0600` in
  `Application Support/Aura/token` and passed to the child through
  `AURA_TOKEN`. Requests with a browser `Origin` are refused (CSRF), as are
  requests whose `Host` isn't loopback (DNS rebinding), and no CORS headers
  are ever emitted. `AURA_NO_AUTH=1` exists for local development and says so
  in the banner; widening the bind needs an explicit `AURA_ALLOW_REMOTE=1`.
- **Thread-correct by construction (v0.5):** the HTTP handlers are *server*
  threads; everything they touch on the orchestrator is scheduled — futures
  resolved with `call_soon_threadsafe`, bus delivery re-dispatched onto the
  owning loop, config changes applied *on* the loop (where wake/STT rebuilds
  are safe). The v0.5 release fixed a real deadlock this rule catches
  (confirm-posted-from-a-browser-thread hung the session forever) and
  regression-tests it over real HTTP.
- The endpoint surface: input (`/api/input|confirm|cancel|correct|trigger`),
  state (`/api/state|health|history|skills|metrics|config`), the consent
  layer (`/api/permissions*`), live settings (`/api/config` — validated,
  persisted, hot-applied), setup (`/api/setup/install|setup/step` — the
  install *starts* and streams progress over SSE instead of holding an HTTP
  request open for minutes), and wake/training control (`/api/wake*`).
- One SSE stream (`/api/events`) drives everything in the app: state, live
  transcripts, plans, proposals, action results, setup progress, log lines.

### 7. Permissions (`permissions.py`, Setup wizard)
Aura treats TCC as *the* consent system, not an obstacle. Every check is
honest and live: Accessibility, microphone via whether Aura's own audio bridge
opened, automation via a harmless AppleEvent actually sent (the consent dialog
is the feature), whisper/Laya readiness via real binary probes and a live
checkpoint load. The Setup wizard renders these as cards with deep links
(`x-apple.systempreferences:…`) into the exact Privacy panes, a progress bar,
and a Check-again loop. Nothing is faked, anywhere.

Accessibility is the one macOS makes hard to read. `AXIsProcessTrusted()` is
answered from a **per-process cache** that a long-lived process fills once and
keeps, so a grant made while Aura runs is invisible to it. Two answers
therefore, both live:

- The **engine** reads from a short-lived child process (a fresh cache) and
  polls every ~2 s while ungranted, publishing an `accessibility` event the
  moment the answer changes — so the panel acknowledges a grant by itself.
- The **app** probes with a listen-only `CGEvent` tap, which cannot be answered
  from that cache, and falls back to `AXIsProcessTrusted()`. It also observes
  `com.apple.accessibility.api` and polls.

Because TCC files the grant under the app that *owns* the process, an engine
started from a terminal is granted as that terminal. Aura names that identity
in the message instead of only saying "not granted".

### 8. The app (`macos/` — AuraCore + AuraMenuBar)
Two Swift targets in one package (`swift build`, no Xcode project):

**`AuraCore`** — Foundation only, so it compiles and unit-tests in seconds
and has no UI dependencies:
- `EngineClient`: a typed async client for every endpoint, plus a
  **self-healing SSE stream** (reconnects with backoff; the UI has one code
  path for “everything that happens”);
- `EngineSupervisor`: owns the Python process — discovers the engine
  (remembered folder → installer copy), **adopts** an engine that is already
  running instead of fighting for the port, reclaims the port from a *stale
  Aura engine* (and refuses to touch anything that isn't ours), restarts with
  bounded exponential backoff, and never leaves the child behind on quit;
- `AppPaths`, `AccessToken` (0600), `AuraLog` (rotating at 5 MB, with an
  in-app tail reader), `Prefs`, and the typed models of the engine's JSON.

**`AuraMenuBar`** — the product: AppKit + SwiftUI, `LSUIElement` (no Dock
icon, one instance):
- `NSStatusItem` whose icon mirrors the engine state (ready / working /
  needs-your-OK / attention), an `NSPopover` hosting the SwiftUI panel (orb,
  transcript, reply, **native confirmation card** with ⌘↩ / Esc and a live
  countdown of the engine's 45 s answer window, composer, suggestions,
  Activity, My data), and a right-click menu wired to the delegate;
- **a pending question always reaches the user**: the card when the panel is
  open, a system notification with Run / Cancel when it isn't — and because a
  transient popover closes on any click elsewhere, the close itself
  re-delivers a still-pending proposal as a notification (once per token).
  When macOS won't show notifications (denied — an ad-hoc rebuild orphans the
  grant exactly like Accessibility), Aura presents its own panel instead and
  Setup shows the permission as a capability row with a deep link into
  System Settings › Notifications. Authorization is read live
  (`getNotificationSettings`), never assumed;
- a Settings window with eight sections driven by the engine's own state
  (General, Voice, Understanding, Safety, Permissions, Wake Phrase, Activity,
  About) and a first-run onboarding window that explains what stays local
  before asking macOS for the two permissions;
- global ⌥Space via Carbon `RegisterEventHotKey` (no permission needed),
  `SMAppService` for “Start at Login”, `AVFoundation`/`AXIsProcessTrusted`
  for the consent dialogs the app owns.
- **No WebKit anywhere** — CI fails the build if `import WebKit` or
  `WKWebView` reappears under `macos/Sources`.
- `scripts/make_app.sh` assembles and signs `Aura.app` — ad-hoc by default, or
  with a stable identity via `AURA_SIGNING_IDENTITY`, which is what keeps an
  Accessibility grant alive across rebuilds (an ad-hoc designated requirement
  is that build's cdhash, so TCC's recorded grant stops matching the next
  binary). The icon is generated by `scripts/make_icon.py`.

### 9. Logging (`log.py`) — the engine explains itself
One place configures three sinks, so "what happened?" has one answer:

- **stderr** — the app's `EngineSupervisor` pipes it into
  `~/Library/Logs/Aura.log` (what a bug report needs);
- **`<data dir>/aura.log`** — a rotating file (2 MB × 3) the engine can read
  back via `aura.log.tail()`, exposed as `GET /api/log?tail=200`;
- **the event bus** — each record above `logging.event_level` is mirrored as a
  `log` event, so the app's Activity feed shows the engine's own log lines.

`log_exception()` is the workhorse: it logs the traceback *and* returns the
short form (`"AttributeError: 'Router' object has no attribute 'ask'"`) that a
reply or a status endpoint carries. Unhandled exceptions on the orchestrator's
loop go through `install_exception_hooks()`. Level is a live setting
(`[logging] level`, `AURA_LOG_LEVEL`). The rule is enforced, not aspirational:
no layer below the gate may swallow an exception, and `python -m aura doctor`
plus `python -m aura laya-check` exist to prove the model path end-to-end from
a terminal.

## Configuration
`config.default.toml` → user `config.toml` → `runtime.toml` (written by the
app) → `AURA_*` env vars. The `auto` profile resolves to **mac** on macOS
and **demo** elsewhere — but off-Mac the demo *base* is applied first and
overridable, so developers can point the brain at another Laya checkpoint
(`laya.checkpoint_dir`) without touching the profile. `LIVE_FIELDS` marks the safe subset the UI may change
at runtime (`POST /api/config` validates type + membership, persists to
`runtime.toml`, hot-applies on the loop); restart-required fields are
refused with a friendly message.

## Testing
**Engine (Linux CI, `pytest`):** 490+ tests cover the JSON parser, the rule
layer, the **Laya adapter against the published API** (a fake `laya` module
implements `Router.predict`/`predict_batch`, so the real code path runs
off-Mac), routing/degradation, the **planner's fallbacks** (dead endpoint ⇒
degraded plan that still acts), the **session guard** (planner crash ⇒ session
still answers, with the error named), the **per-skill deadline** (a wedged
Accessibility call fails as itself instead of eating the session), logging
(file/bus/tail/`/api/log`), wake fallback honesty, the **train → detect
round-trip** (studio-shaped captures must wake the running detector — the
regression behind "my phrase never fires"), safety verdicts, the Laya
heuristic + example buffer, memory/FTS/preferences, **access control** (token
required, browser `Origin` and foreign `Host` refused, no HTML surface), live
config round-trips and refusals, and **full orchestration over real HTTP**
(typed session, confirmation flow, cancel flow, busy-state hints, feedback
recording). The dry-run bridge makes the whole product testable on Linux —
the same code path a Mac runs.

**App (macOS CI, `swift test`):** `AuraCoreTests` cover the JSON model, the
SSE parser (multi-line data, keepalives, whole-block feeds), the client
(token header on every request, 401 ⇒ “another engine holds this port”,
403 ⇒ the engine's own message, decoding of state/config), path and token
handling, and the shortcut map. A second job builds the release binary,
runs those tests, assembles `Aura.app`, and validates the bundle
(executable bit, `plutil`, `LSUIElement=true`, and the WebKit ban).

**Anywhere:** `scripts/check_swift_syntax.py` parses every Swift file with
tree-sitter in about a second, so a syntax error is caught on Linux instead
of costing a macOS runner cycle.
