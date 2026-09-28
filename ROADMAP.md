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

## v0.2 — Real Mac bring-up ✅
- [x] Permissions onboarding wizard (live TCC detection, System Settings
      deep links, AppleScript probe, readiness checks)
- [x] Menu-bar shell (Swift/AppKit: popover + WKWebView, global ⌥Space,
      process babysitter, logs, login item, `make_app.sh` bundler)
- [x] Always-listening wake mode with phrase gate (second factor), live
      manual↔always-on switch from the UI, persisted to runtime.toml
- [x] Lightweight enforcement: lazy STT loading + idle model unload,
      config hot-reload (safe fields live, restart-fields refused),
      `/api/metrics` self-observation (RSS, engines, example counts)
- [x] Install-time fetch: install_mac.sh downloads every model up front
      (whisper ggml, wake-word models, qwen3:4b) — nothing later at runtime
- [ ] Field-test on Apple Silicon (the only item that needs your Mac:
      mic input, Homebrew whisper.cpp, Ollama planner, real executors)

## v0.3 — Laya element-picking ✅ (core shipped)
- [x] `aura/ax.py` — the Accessibility substrate: real AXUIElement reader
      (pyobjc) + deterministic MockAXTree; press / focus / insert behind one
      interface
- [x] `aura/picker.py` — coarse-to-fine element picking (≤16-option chunks,
      two winning chunks, fine scoring): Laya-scored when the backend is
      real, deterministic heuristic otherwise; below-threshold → honest
      "did you mean …" instead of a wrong click
- [x] Skills: `ax.click`, `ax.type_into`, `ax.read_screen` — Aura can now
      act inside any app that exposes accessibility labels, by name
- [x] Safety integration: destructive labels (delete/purchase/send…) flip
      ax.click into the confirmation flow automatically
- [ ] Generate skill-routing + safety datasets; wire the `laya` pip backend
      into daily use; publish accuracy/latency benchmarks
- [ ] Ship `scripts/nightly_laya.py` v1 end-to-end: export → on-device
      fine-tune → adapter hot-swap; measure before/after on a held-out set
- [ ] Confidence UX upgrade: "I'm only 54% sure — did you mean X?" as an
      interactive clarification (currently a spoken suggestion)
- [ ] Laya element-picking with the real Laya fine-tune (v10s-style
      checkpoint over AX labels; coarse-to-fine already wired)

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
