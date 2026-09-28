# Roadmap

Ship order matters: each stage is usable on its own, and every stage widens
the moat (offline + personal + fast) before Apple's Siri V2 closes the
mainstream gap.

**Current release: v0.6.0 — the native macOS app** (✅ shipped; see below).

## v0.1 — Foundation ✅
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
- [x] Menu-bar shell (Swift/AppKit: menu-bar popover, global ⌥Space,
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

## v0.4 — Ownable product ✅ (shipped)
- [x] **Wake Phrase Studio**: train your own phrase inside the app — type it,
      say it a few times when prompted, done in seconds. Spectral-template
      trainer (80-dim log-band embedding, length-normalized to a shared
      1 s window; calibrated cosine threshold; synthesized + `say`-rendered
      negatives; sample quality judge with friendly guidance)
- [x] `TemplateWakeEngine` — detection on the user's own voice, refractory,
      live rebuild the moment training finishes (no restart)
- [x] **In-app installer** (Setup panel): whisper.cpp model download with
      visible progress, environment probe, hot-swapped STT — no commands
- [x] **Installed-app story**: `install_mac.sh` ends with Aura.app in
      /Applications, opened; after install the terminal never appears again
- [x] **Zero-demo on user machines**: the developer-build banner and every
      simulated surface appear only off-Mac; permissions drive the gating
- [x] Full Apple-grade copy pass over every UI string; version 0.4.0

## v0.5 — Ship-quality product ✅
The theme: **a real user, on a real Mac, with zero patience for "almost
works."** Every item below is something that could be shipped, plus the
fixes that make it feel like it is.

- [x] **One-command install** — `./scripts/install.sh`: brew deps →
      Ollama + qwen3:4b → speech model → engine + venv → Aura.app in
      /Applications → open. Idempotent, resumable, no second terminal
- [x] **Native first-run** — Aura.app opens a *Welcome to Aura* window
      instead of a browser tab; macOS asks for Microphone (bundle usage
      string) and Accessibility proactively; no more "opened a URL at
      127.0.0.1:7331" (the window itself became fully native in v0.6)
- [x] **Settings that work** — every card is live: re-request any
      permission, open the exact System Settings pane, test Automation,
      toggle the voice and confirmation modes, switch the brain engine,
      change the data folder; changes persist and hot-apply
- [x] **Honest permission UI** — Setup/Settings render the *real* state of
      microphone / accessibility / automation / models on this machine,
      with the exact action for each (grant, open settings, download,
      install) — nothing simulated on a Mac
- [x] **A brain that degrades, not dies** — HybridPlanner: local qwen3
      (Ollama) when up, built-in skills + visible "running on basics"
      badge when down; "open youtube" was doing nothing before, now it
      always answers
- [x] **The session guard** — a planner crash can no longer leave a
      command stuck mid-flight; the session answers with built-in skills
      instead of hanging
- [x] **Wake-word honesty** — missing models are detected, the UI says so,
      and the fallback happens gracefully (manual mode) with a note
- [x] **Thread-correct engine** — confirm/feedback/config events from the
      HTTP thread now schedule onto the orchestrator loop (a real
      deadlock behind "I clicked Run and nothing happened" is fixed and
      regression-tested)
- [x] **Menu-bar app as a product surface** — full right-click menu
      (Open Aura, Wake, Setup & Permissions, Restart Engine, Choose Aura
      Folder, Start at Login, Activity Log, Quit), icon that mirrors live
      state (ready/busy/needs-OK/offline) with recovery reload — all of it
      native UI by v0.6, with no web view in the shot
- [x] **App identity** — generated icon (`scripts/make_icon.py` →
      ui/icon.png / .icns, no binary hand-maintenance), real bundle
      strings (usage descriptions, category, copyright), v0.5.0
- [x] **Doctor that speaks English** — `python -m aura doctor` live-checks
      mic / accessibility / wake models / speech model / brain / app
      install; `--version`
- [x] 144 tests, all green — including the ship regressions above

## v0.5.1 — Speak the form, fill the form ✅
The theme: **forms in any browser, filled from your voice — seamless,
fast, light, powerful.** No extension, no click-through: Aura reads the
live accessibility tree, so it works wherever a normal field shows a
normal label.

- [x] **`ax.fill_form`** — “*fill this form: name John, email me at
      smith dot com*”: scans the frontmost window's fields, maps the
      dictation onto labels (synonyms: mail/email; grammar: “*set city to
      Portland*”, “*the mail is …*”, ordinal “*first field …*”, bare
      positional lists), and types every value with original casing kept.
      Heuristic-first for speed; an offline LLM pass refines only when the
      heuristic matched nothing and a brain is online
- [x] **`ax.read_form`** — “*what fields does this form have?*” lists the
      fields and the ending button in plain English
- [x] **`ax.dictate`** — “*type 123 Main Street*”: plain dictation into
      the focused field, ~100 ms, no tree read — the fast path
- [x] **Honest by design** — unmentioned fields stay empty and are
      reported (“Still empty: …”); values are never invented
- [x] **Send is always your call** — filling auto-runs (it's safe);
      pressing submit/send/save/sign-up is a separate confirm-gated
      action; “*fill this form: … and press submit*” plans both
- [x] **Sign-up / register / subscribe are confirm-gated** in the
      destructive-argument list — a voice click can never sign you up
- [x] 33 new tests: scan, parse (grammar/synonyms/case/ordinals/position),
      LLM refine + failure, read, fill, dictation, routing, the safety
      gate, and a full orchestrator end-to-end

## v0.6 — Native macOS app ✅ (this tree)
The theme: **nothing in a browser.** The UI is AppKit + SwiftUI, the product
surface is the menu bar, and the engine got the security model a localhost
server needs.
- [x] **Native panel** — an `NSPopover` hosting SwiftUI: orb, transcript,
      reply, a real confirmation card (⌘↩ / Esc), composer and suggestions.
      No `WKWebView` anywhere, and CI fails the build if one reappears
- [x] **Menu-bar product** — status icon mirrors engine state; right-click
      menu (wake · settings · restart · choose engine folder · log · login ·
      quit); global ⌥Space re-registered the moment the shortcut changes
- [x] **Settings + onboarding** — eight live sections (General · Voice ·
      Understanding · Safety · Permissions · Wake Phrase · Activity · About)
      and a first-run window that explains what stays local *before* macOS
      asks for Microphone and Accessibility
- [x] **Loopback API, hardened** — `X-Aura-Token` on every request; browser
      `Origin` and foreign `Host` refused; JSON-only surface (no HTML, JSON
      404/405); `/api/setup/install` answers immediately and streams progress
      over SSE instead of holding a request open for minutes
- [x] **Engine supervisor** — adopts a healthy engine, reclaims the port from
      a stale *Aura* process, refuses to touch anything else and names the
      occupant, bounded exponential backoff, no orphan child on quit
- [x] **Verified on every push** — 207 engine tests, ruff clean, Swift
      syntax gate, `swift build` + 16 Swift tests, `Aura.app` assembled and
      its bundle validated (LSUIElement, usage strings, WebKit ban)

## v0.7 — Depth
- [ ] Browser extension (MV3) + CDP: read/act on DOM, multi-tab flows
- [ ] Window management skills (positions, spaces) via AX
- [ ] Procedures: "watch me do this once" → recorded, editable, replayable
      workflows (OpenAdapt-style, voice-triggered)
- [ ] Calendar / Mail / Notes / Reminders skills via AppleScript + EventKit
- [ ] Optional local VLM fallback for AX-hostile apps (Moondream-class)

## v0.8 — Polish & distribution
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
