# Roadmap

Ship order matters: each stage is usable on its own, and every stage widens
the moat (offline + personal + fast) before Apple's Siri V2 closes the
mainstream gap.

## v0.1 — Foundation ✅ (this tree)
- [x] Orchestrator state machine with confirmation as a real state
- [x] Voice front-end interfaces: mic, wake (pretrained + custom models), VAD,
      STT (whisper.cpp / faster-whisper), TTS (`say`)
- [x] Planner contract + tolerant parsing + local OpenAI-compatible client
- [x] Laya gate (real backend when `laya` is importable; heuristic fallback)
      with match/destructive scoring and thresholds
- [x] Safety: blocklist → skill manifest → calibrated gate; plan-before-run
- [x] 17 declared skills across system / browser / clipboard / memory
- [x] Memory: sessions, FTS search, durable preferences, example buffer
- [x] Learning loop plumbing: 👍/👎 → weighted supervision → JSONL export
- [x] Zero-dependency UI: orb, transcripts, proposals, timeline, skills,
      settings — SSE-live
- [x] 48 tests, all green; dry-run bridge runs the product on any OS

## v0.2 — Real Mac bring-up (config, not code)
- [ ] Field-test on Apple Silicon: mic input, whisper.cpp via Homebrew,
      Ollama qwen3:4b planner, real executors end to end
- [ ] Permissions onboarding flow (Accessibility, Microphone, Automation)
      as a first-run wizard in the UI
- [ ] Always-listening mode with phrase gate; refine thresholds from logs
- [ ] Idle model unload; energy check (<1% CPU idle, <250 MB idle RSS)
- [ ] `launchd` plist + menu-bar shell (Swift, thin: status item + WKWebView)
- [ ] Settings hot-reload (watch config.toml; no restart)

## v0.3 — Laya, for real
- [ ] Generate skill-routing + safety datasets (synthetic + curated); wire the
      `laya` pip backend into daily use; publish accuracy/latency benchmarks
- [ ] Ship `scripts/nightly_laya.py` v1: export → on-device fine-tune →
      adapter hot-swap; measure before/after on a held-out set
- [ ] Laya element-picking for the Accessibility tree (coarse-to-fine chunking
      after the `laya-browser` v3 recipe) — clicks *any* app's UI by label
- [ ] Confidence UX: "I'm only 54% sure — did you mean X?" instead of raw asks

## v0.4 — Depth
- [ ] Browser extension (MV3) + CDP: read/act on DOM, forms, multi-tab flows
- [ ] Window management skills (positions, spaces) via AX
- [ ] Procedures: "watch me do this once" → recorded, editable, replayable
      workflows (OpenAdapt-style, voice-triggered)
- [ ] Calendar / Mail / Notes / Reminders skills via AppleScript + EventKit
- [ ] Optional local VLM fallback for AX-hostile apps (Moondream-class)

## v0.5 — Polish & distribution
- [ ] Signed + notarized DMG with bundled models; auto-update channel
- [ ] Voice models: Kokoro premium voices; on-device voice-match (optional)
- [ ] Multi-mac sync of preferences via user's own iCloud Drive folder (files
      only — still no server of ours)
- [ ] Documentation site + benchmark page (latency tables vs screenshot agents)
- [ ] Open-source community: skill SDK, contributing guide, good first issues

## Later bets
- Apple Foundation Models framework as an alternate planner backend (free,
  on-device, no download) — behind the same interface
- Streaming STT partials for sub-second perceived latency
- Smoke tests on macOS CI runners for AppleScript skills
