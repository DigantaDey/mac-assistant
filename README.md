# Aura — your Mac, at your command.

<p align="center">
  <img src="ui/icon.svg" width="72" alt="Aura">
</p>

**Aura is a private, offline, voice-controlled assistant for macOS.**
You choose the activation phrase. Aura listens for it, understands you on-device,
shows you a plan before it touches anything — and gets sharper every week,
because every confirmation and correction you give becomes training data for
its [Laya](https://huggingface.co/convaiinnovations) decision core.

No cloud. No API keys. No telemetry. Idle footprint under ~200 MB.

```text
 you:  "hey aura … open Spotify and set the volume to 30"

       ◉ wake word (your phrase, on-device)
       │
       ▼
       whisper.cpp          speech → text, on the Neural Engine
       │
       ▼
       Qwen3-4B (MLX)       a small local model plans with real skills
       │
       ▼
       Laya gate            every action scored: match? destructive?  ← ~33 ms
       │                        low confidence ⇒ it asks, never guesses
       ▼
       skills               apps · windows · volume · browser · clipboard …
       │
       ▼
       `say` + timeline     spoken reply, and a log you can correct
```

---

## Try it in 60 seconds (works on any machine)

```bash
git clone https://github.com/DigantaDey/mac-assistant && cd mac-assistant
python3 -m aura serve          # demo profile: deterministic, safe, no deps
# → http://127.0.0.1:7331
```

You'll get the full product — orb, transcripts, plan proposals, confirm/cancel,
activity timeline with a 👍/👎 learning loop — with actions **simulated**
(nothing touches your system). Try:

- `open spotify and set volume to 30`
- `search for flights to goa` · `open github.com`
- `empty the trash` → watch Aura **stop and ask first**
- `remember that my editor is Zed` → it keeps that forever

## Run it for real on a Mac

```bash
# 1. system deps
brew install portaudio whisper-cpp ollama
ollama pull qwen3:4b                      # the planner brain (~2.5 GB)

# 2. python env
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[mac]"                   # sounddevice, openwakeword, laya, httpx…

# 3. go
python -m aura doctor                     # capability check
python -m aura serve                      # real mic, real executors
```

Then grant permissions when macOS asks (**System Settings → Privacy & Security**):
**Accessibility** (drive apps), **Microphone** (hear you), and per-app
**Automation** (AppleScript). Aura's UI walks you through it.

### Make the wake word yours

```bash
python scripts/train_wakeword.py "hey aura"   # trains locally, ~20 min
```

Drop the resulting ONNX where it says, set `wake.mode = "openwakeword"` in
`config.toml`, and Aura will answer to *your* phrase — trained on your machine,
never leaving it.

---

## What's in the box

| | |
|---|---|
| **Wake** | openWakeWord models + a spoken-phrase second factor; custom self-trained models |
| **STT** | whisper.cpp (Metal/CoreML) → faster-whisper → typed input, auto-selected |
| **Plan** | any local OpenAI-compatible server: Ollama, `mlx_lm.server`, llama.cpp, LM Studio |
| **Gate** | [Laya](docs/RESEARCH.md) (Apache-2.0) — calibrated match/destructive scores in milliseconds; deterministic heuristic fallback |
| **Act** | 17 declared skills (system, browser, clipboard, memory) via AppleScript/Accessibility — dry-run bridge everywhere else |
| **Speak** | macOS `say` (zero RAM) or Piper/Kokoro |
| **Learn** | SQLite event store → preference memory → Laya fine-tune buffer → `scripts/nightly_laya.py` |
| **UI** | hand-rolled, zero-dependency, Apple-grade dark interface on `127.0.0.1:7331` — SSE-live |

## Design principles

1. **Private by architecture, not by policy.** Loopback-only server, no
   telemetry, models on disk, data in plain SQLite you can open and delete.
2. **Ask, don't guess.** Calibrated confidence below threshold ⇒ a visible
   proposal with confirm/cancel. Destructive is never "safe".
3. **Lightweight is a feature.** Only the wake model stays resident; STT/LLM
   load on demand and unload when idle.
4. **Every action is inspectable.** A timeline, a log, an audit trail — no
   magic, and corrections feed the next fine-tune.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the layer-by-layer design,
[ROADMAP.md](ROADMAP.md) for where this is going, and
[docs/RESEARCH.md](docs/RESEARCH.md) for the market/feasibility research
that kicked this off.

## Status

**v0.1 — foundation.** Core engine + UI + demo profile: tested (48 tests),
running. Mac bring-up (real mic/executors/Laya) is config, not code — see
ROADMAP for the sequence.

MIT licensed. Built with care.
