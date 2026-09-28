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
│ `say` │  OpenAI-compat local    blocklist          17 declared skills      │
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
  ONNX models (`scripts/train_wakeword.py`). A configurable **spoken phrase
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
- The UI is hand-rolled HTML/CSS/JS — no framework, no build step, no fonts to
  download (system SF stack). One SSE stream drives everything: orb states,
  live transcripts, plan proposals, confirm/cancel (⌘↩ / esc), the activity
  timeline with 👍/👎 correction, skills grid, settings.

## Configuration
`config.default.toml` → user `config.toml` → `AURA_*` env vars. Demo profile
pins safe providers so behavior is reproducible everywhere.

## Testing
48 tests cover the JSON parser, mock planner routing, safety verdicts, the
Laya heuristic + example buffer, memory/FTS/preferences, and **full
orchestration over real HTTP** (typed session, confirmation flow, cancel flow,
busy-state hints, feedback recording). The dry-run bridge makes the entire
product testable on Linux CI — the same code path a Mac runs.
