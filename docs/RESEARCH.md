# Research Report: Offline Voice-Controlled MacBook Assistant on Laya

**Date:** 2026-09-28
**Question:** Does a private, fully-offline, lightweight, voice-controlled Mac assistant built on the open-source Laya decision model already exist? What is the market demand? Is it technically feasible?

---

## 1. Executive Summary

| Question | Verdict | Confidence |
|---|---|---|
| Does this exact product already exist? | **No — closest is ~70% there (Fazm). The full combination is an open niche.** | High |
| Is there market demand? | **Yes, a growing niche.** Macro tailwinds are strong (voice AI ~31–33% CAGR; Apple's personalized Siri delayed ~18 months and moving to Google Gemini cloud). Realistic SOM for 2–3 yrs: **$2–20M ARR**, plus high strategic/learning value. | Medium-High |
| Is it technically feasible offline & lightweight? | **Yes on Apple Silicon (M1+).** Every layer has a proven offline component. Main caveat: **Laya cannot be used zero-shot** — it must be fine-tuned per task, which conveniently doubles as your personalization mechanism. | High (architecture) / Medium (Laya fine-tune effort) |

**The single most important technical finding:** Laya is a *decision model*, not a language model. It never generates text — it answers typed questions ("which element should I click?", "is this action destructive?") with calibrated probabilities in ~33 ms. It **cannot** understand free-form voice commands, plan, or converse on its own. Your product needs a hybrid brain:

```
Voice ──► Wake word ──► STT (Whisper) ──► Small local LLM (intent + planning)
                                                │
                                    ┌───────────┴───────────┐
                                    ▼                       ▼
                              Laya decision layer     Action executors
                        (element choice, tool routing,  (Accessibility API,
                         safety gating — 33 ms,         AppleScript, CDP/browser,
                         calibrated confidence)         shell, system APIs)
                                    │
                                    ▼
                          TTS feedback (Kokoro/Piper/macOS) ──► User
```

Laya is actually a *great* fit for this role — it's exactly the "fast, safe reflex layer" pattern the ecosystem is converging on — but the report below is honest about its limitations.

---

## 2. Laya: What It Is and Isn't

Released **Sept 18, 2026** by Convai Innovations, Apache-2.0, three days after TypeSafe AI's closed **Jev** ("System 1 model" category creator).

**What it does:**
- Non-autoregressive: evaluates typed questions over a "state" (text, email, UI element list, JSON) in a **single forward pass**. Output space is three primitives: `choice` / `score` / boolean — with calibrated probabilities. It never writes text, so it cannot hallucinate an instruction.
- **~32.8 ms** per routed decision vs Jev's 236–276 ms (~7.8× faster). Calibration ECE 0.081 vs Jev's 0.246.
- Three checkpoints on Hugging Face: English 421M (~808 MB), 322M multilingual (100+ languages), 421M typed-decisions specialist. `pip install laya`.

**On-Mac performance (the numbers that matter for this product):**
- **laya-mlx** (Apple Silicon port): under **1 GB RAM**, up to 50× faster than Jev, fully local. Ran on a 16 GB MacBook Air in demos.
- **CoreML port** (FluidInference "FluidUse" v0.2.0): **3.7 ms/decision on M5 Pro**, 99.5% of ops on the Apple Neural Engine.

**Honest weaknesses (from independent reviews):**
- **Zero-shot accuracy is poor: ~0.362** vs a 0.318 random baseline. Fine-tuned accuracy is strong (**0.766**, above the 0.735 teacher ceiling) — but that means every task you point Laya at needs a fine-tune. This is the core engineering investment of the project.
- **Degrades beyond ~20 options** (0.425 on 77-way Banking77) → decisions must be hierarchical/coarse-to-fine (chunk element lists, decide in two passes) — exactly what the `cklxx/laya-browser` fine-tune (v10s checkpoint) does for web pages, and that recipe generalizes to macOS Accessibility trees.
- No text generation ever — so an LLM somewhere in the stack is unavoidable for free-form commands.

**Implication for you:** Laya's cheap fine-tuning (421M params — trainable *on-device*, in minutes not hours) is a feature, not a bug. Every confirmed/corrected action becomes a labeled training example → **the feedback/personalization loop you want maps naturally onto "continually fine-tune a small Laya adapter" + memory**, not onto fine-tuning a 7B LLM (which is an occasional overnight job, not a continuous one).

---

## 3. Landscape: What Already Exists

### 3.1 Closest competitors

| Product | Platform | Voice | Fully offline | Controls whole Mac | Custom wake word | Self-learning | License | Traction |
|---|---|---|---|---|---|---|---|---|
| **Fazm** (mediar-ai) | macOS | ✅ push-to-talk | ✅ (Ollama) | ✅ Accessibility + vision | ❌ | ⚠️ partial (plan preview, persistent context) | MIT | ~340★, 5.2k commits, active (v2.9.89) |
| **Open Interpreter** | Cross | ❌ | ⚠️ `--local` | ⚠️ code-first, GUI secondary | ❌ | ❌ | AGPL-3.0 | ~57k★ |
| **UI-TARS Desktop / Agent TARS** (ByteDance) | Cross | ❌ | ⚠️ own VLM needs GPU | ✅ screenshots | ❌ | ❌ | Apache-2.0 | ~38.8k★ |
| **CUA** (trycua) | Cross | ❌ | ⚠️ | ✅ (in sandboxed VMs) | ❌ | ❌ | MIT | ~22.1k★ |
| **Agent S3** (Simular) | Cross | ❌ | ⚠️ research-grade | ✅ | ❌ | ⚠️ reflection/experience | Apache-2.0 | ~12.2k★ |
| **self-operating-computer** | Cross | ❌ | ⚠️ Ollama | ✅ screenshots | ❌ | ❌ | MIT | ~10.3k★ |
| **OpenAdapt** | Cross | ❌ | ⚠️ partial | ✅ record/replay RPA | ❌ | ✅ **learns by demonstration** | MIT | ~1.9k★ |
| **Agent! (macOS26)** | macOS | ✅ hotkey + iMessage | ✅ possible | ✅ Accessibility, 51 app bridges | ❌ | ⚠️ remembers preferences | MIT | small |
| **OS-Copilot** | Linux/macOS | ❌ | ⚠️ | ⚠️ | ❌ | ⚠️ | Apache-2.0 | ~2.8k★ |
| **Apple Voice Control** (built-in) | macOS | ✅ always-on | ✅ | ⚠️ accessibility grammar only, no LLM, no apps beyond AX | ✅ (Vocabulary) | ❌ | Proprietary | Ships with macOS |
| **Siri / Apple Intelligence** | macOS | ✅ | ⚠️ hybrid | ❌ (delayed ~18 mo; App-Intents-gated) | ❌ fixed "Siri" | ❌ | Proprietary | 940M devices enabled |

### 3.2 Gap analysis — your requested feature set vs. the field

Your six requirements: **(1) whole-computer control (system + browser + universal apps), (2) voice-first, (3) user-defined activation command, (4) fully offline, (5) lightweight, (6) feedback loop that personalizes over time.**

- **Fazm** hits requirements 1, 2 (as push-to-talk), 4 — but no user-set wake word, no decision-model safety/confidence layer, and no genuine self-learning loop. It is the product to beat and the best proof the concept resonates (it got significant r/LocalLLM, r/ollama, r/MacOS attention in March 2026).
- **OpenAdapt** is the only one with real learning-from-demonstration, but it's clunky, cross-platform (not Mac-tuned), and not voice-first.
- **Apple Voice Control** is offline + always-on but is a dumb command grammar — no LLM, no universal app actions, no adaptivity.
- **Nobody** combines a user-trained wake word + Laya-style calibrated decision layer + continuous on-device personalization.
- Notably, **no existing tool uses Laya as its decision core** — the laya-browser-agent repo (Sept 2026) wires Laya into browser agents, but a full-Mac assistant on Laya does not exist. You'd be first.

### 3.3 Competitive timing window

Apple's personalized Siri was demoed June 2024, delayed ~18 months, and as of Feb 2026 is *still* slipping (iOS 26.4 → likely spread into iOS 27), with Apple signing a multiyear **Google Gemini** cloud partnership for its foundation models. Every report of "Siri leans on Gemini cloud" increases demand for a genuinely local alternative. But the window is **18–30 months**: Siri V2 + App Intents + the Foundation Models framework will progressively close the "assistant that acts across apps" gap. A third party will still be able to go further on (a) non-App-Intents apps, (b) browser control, (c) user-owned customization — but you should assume Apple ships "good enough" for mainstream users and target power/privacy users.

---

## 4. Market Demand

### 4.1 Macro numbers

- AI in voice assistants: **$6.13B (2026) → $18.16B (2030), 31.2% CAGR** (Research and Markets).
- Voice assistant applications: **$9.62B (2026) → $30.42B (2030), 33.3% CAGR** (Research and Markets).
- AI voice agents segment: **$3.51B in 2026** (Grand View Research) → **$47.5B by 2034** (Market.us).
- **8.4B voice-enabled devices** active worldwide; 157M Americans using voice assistants by end of 2026.
- "Privacy-focused voice processing" is explicitly listed as a major market trend for 2026–2030.

### 4.2 The Mac-specific slice

- Mac business at record levels: **$10.4B revenue in Apple's June-2026 quarter (+29% YoY)**, with AI labs notably buying Macs; Apple Intelligence enabled on **~940M devices / 410M DAU** across iPhone/iPad/Mac.
- **79% of Mac users actively use AI tools**; local-AI-on-Mac is a proven *monetizable* category: MacWhisper ($30–60 one-time), LocalChat ($49.50 lifetime), Superwhisper (subscription), LM Studio (free but venture-funded for ecosystem reasons).
- The LocalLLM/ollama Reddit communities (100k+ members each) routinely upvote local Mac assistants — Fazm's launch threads and AIYO Wisper's launch both hit the front page. Demand signal for "local-first assistant" is real and vocal.

### 4.3 Honest niche sizing

| Funnel | Estimate |
|---|---|
| Active Macs | ~120M+ |
| Apple-Silicon Macs that can run the stack (8 GB+) | ~70–90M |
| Privacy-motivated / local-AI-curious Mac users | ~1–3M |
| Reachable via LocalLLM communities, Product Hunt, HN (SAM) | ~100–500k |
| Realistic users in 2–3 yrs with good execution (SOM) | **50k–250k** |
| Revenue at $40–80 one-time or $8–15/mo | **≈ $2–20M ARR** |

**Verdict:** a genuine niche, not a unicorn market — but a defensible one with high strategic value: (1) the timing window before Siri V2, (2) Laya gives you a technical differentiator nobody else has shipped, (3) open-source-core + paid-convenience (signed notarized app, pre-bundled models, auto-update, premium voices/skills) is a proven monetization model in this exact ecosystem. It's also an exceptional portfolio/credibility project regardless of revenue.

---

## 5. Technical Feasibility

**Overall: feasible today, fully offline, on any Apple Silicon Mac with 8 GB RAM** — if (and only if) the architecture is disciplined about model sizes and ANE offload.

### 5.1 Proven offline stack, layer by layer

| Layer | Choice (lightweight) | RAM | Latency | License |
|---|---|---|---|---|
| **Wake word (user-set activation)** | **openWakeWord** (Apache-2.0) or ViolaWake — custom wake words trainable by the user in 20+ languages, ONNX, runs on-device. *Avoid Porcupine: free tier discontinued June 30, 2026.* | ~10–50 MB | always-on, low single-digit % CPU (ANE offloadable) | Apache-2.0 |
| **STT** | whisper.cpp (small/turbo, Metal + CoreML/ANE) — or Apple's on-device Speech framework for near-zero footprint | 0.5–1.5 GB (or ~0 w/ Apple Speech) | <1 s for short commands on M-series | MIT |
| **Understanding / planning** | Small LLM, 4-bit: **Qwen3-4B** (~2.5 GB) or 8B (~4.5 GB) via MLX/llama.cpp; optional Apple Foundation Models (~3B, free, on-device) | 2–5 GB when active | first token ~0.3–1 s | Apache-2.0 etc. |
| **Decision layer** | **Laya via laya-mlx** (<1 GB) or CoreML/ANE port (3.7 ms on M5 Pro) | <1 GB | **~3–33 ms** | Apache-2.0 |
| **Perception** | macOS Accessibility tree (**AXUIElement: ~50 ms to read, 40–100× faster than screenshots, text-only = private**) + selective screenshot/VLM fallback (Moondream-class) for AX-hostile apps (games, some Electron) | negligible / 1–2 GB fallback | ~50 ms | — |
| **Browser control** | Native extension + CDP (Chrome DevTools Protocol) — DOM access is more reliable than screen-vision; laya-browser's v10 recipe handles element choice | negligible | ms | — |
| **System actions** | AppleScript/JXA + Accessibility + native Swift (window mgmt, Finder, System Settings, media, files) | negligible | ms | — |
| **TTS feedback** | macOS AVSpeechSynthesizer (zero extra RAM, offline) or Kokoro-82M (best quality, Apache-2.0, <300 ms, ~0.5 GB) / Piper (<100 ms, CPU) | 0–0.5 GB | <0.3 s | MIT/Apache-2.0 |
| **Memory** | SQLite + sqlite-vec (local vector store), facts/preferences/experience episodes | negligible | ms | — |

**End-to-end realistic latency on an M-series Mac: wake word → response begins in ~1–1.5 s** (comparable builds are documented at 1–2 s on equivalent hardware).

### 5.2 Resource budget (the "lightweight" requirement)

- **Idle (listening for wake word only):** <200 MB RAM, ~1–3% CPU. Achieved by keeping *only* the wake-word model resident; load STT/LLM lazily on activation, unload on sleep timeout.
- **Active:** peaks of ~3–6 GB RAM depending on LLM size (4B vs 8B), <0.5 GB of which is Laya. On a 16 GB Mac this coexists fine with normal work; on 8 GB use the 4B model + Apple Speech STT.
- Battery: always-listening wake word on the ANE is milliwatt-scale; the LLM should never stay warm — hot-standby only while a session is active.

This satisfies "lightweight without consuming much resources" far better than screenshot-driven agents (UI-TARS/Agent S3 class), which hold multi-GB VLMs and burn seconds per UI frame.

### 5.3 The feedback / self-learning mechanism (your differentiator)

A pragmatic three-tier loop, all on-device:

1. **Tier 1 — Memory (ship in v1):** every command, plan, outcome, and correction stored locally; retrieved as few-shot context. Cheap, safe, immediately useful ("remember that when I say 'work mode' I mean Slack + Spotify + Do Not Disturb").
2. **Tier 2 — Laya adapter fine-tuning (the core loop):** confirmed actions = positive labels, corrections = negative labels. Fine-tune a small LoRA adapter on the 421M Laya nightly (minutes on-device). Because Laya is tiny and calibrated, this genuinely improves routing/element-picking/safety-gating over time — *measurable* personalization, not vibes. Confidence thresholds gate risk: low-confidence decisions ask the user instead of guessing.
3. **Tier 3 — Occasional LLM QLoRA (optional, "deep" personalization):** MLX makes on-device LoRA of a 7B model feasible (~7 GB peak, ~90 min for 5k examples — an overnight job, only every few months). Optionally also learn repeatable procedures from demonstration, OpenAdapt-style ("show me once, I'll remember the workflow").

Also required for trust: a visible plan before execution, confirmation for destructive actions (delete, send, purchase), per-skill permission toggles, and a full local audit log. Community feedback on Fazm confirms safety/transparency is the #1 user concern for this category.

### 5.4 Risks & mitigations (the honest list)

| Risk | Reality | Mitigation |
|---|---|---|
| **Laya zero-shot is too weak** (~0.36) | Confirmed by independent review | Bootstrap with synthetic + curated labeled data per skill; ship v1 with Tier-1 memory only while the Laya fine-tune matures |
| **>20 options degrade accuracy** | 0.425 on 77-way classification | Hierarchical decisions (coarse→fine chunking), the laya-browser v3 recipe |
| **macOS TCC permission friction** (Accessibility, Microphone, Screen Recording, per-app Automation) | Real onboarding hurdle; notarization needed | Guided permission onboarding flow; signed + notarized app; document clearly (SOFAs are well understood) |
| **AX-hostile apps** (games, some Electron/custom UIs) | Accessibility tree incomplete | Browser extension/CDP for web; optional screenshot + small VLM fallback mode (heavier, off by default) |
| **macOS updates churn AX trees** | Ongoing maintenance tax | Element matching by role+label+bundle-id (coordinate-free), regression test harness |
| **Apple closes the gap** (Siri V2 + App Intents + Foundation Models, 2026–27) | Likely for mainstream use cases | Moat = cross-app universal control, browser, full offline, user-owned wake word & customization; privacy positioning strengthens as Siri leans on Gemini cloud |
| **Scope explosion** ("control the entire computer") | Classic failure mode | Ship narrow: system navigation + browser + top-10 apps first; skill/plugin SDK for the rest |

### 5.5 Effort estimate

- **MVP (wake word → STT → 4B LLM → Accessibility/AppleScript actions → TTS, 3–5 skills, Tier-1 memory):** ~8–12 weeks for one strong dev using existing pieces (openWakeWord, whisper.cpp, MLX, AX tooling).
- **Laya decision layer + feedback loop (Tier 2):** +2–3 months (dataset generation is the bulk of the work).
- **Production polish (notarized app, onboarding, 20+ app skills, self-updater, docs):** ~6–9 months total to something you'd confidently put in strangers' hands.

---

## 6. Recommendation

**Build it — as an open-source-core Mac app, in this order:**

1. **v0.1 (weeks 1–12):** openWakeWord (user-trained activation phrase) → whisper.cpp → Qwen3-4B (MLX) → Accessibility/AppleScript/JXA + browser-CDP executors → AVSpeechSynthesizer/Kokoro feedback → SQLite memory. 10–15 high-value skills (window/system control, Finder, Safari/Chrome, Mail, Music, Calendar, Notes, terminal).
2. **v0.3:** Laya decision layer for tool routing + safety gating + element picking (fine-tuned; MLX backend, CoreML where available). Confidence-gated confirmations. Local audit log.
3. **v0.5:** Tier-2 learning loop (nightly Laya adapter fine-tune from feedback), workflow memory ("show me once"), per-skill permissions UI.
4. **Monetization:** free OSS core; paid tier = signed/notarized auto-updating app, pre-bundled optimized models, premium voices, skill packs. Publish benchmarks (latency, RAM, decision accuracy vs Fazm/screenshot agents) — measurable superiority is your marketing.

**Why this wins vs. what exists:** the only products near this vision are either (a) cloud-tethered, (b) resource-hungry screenshot-VLM agents, (c) dumb offline grammars, or (d) missing the personalization loop. A ~1–2 GB-active-footprint, fully-offline, Laya-gated, self-improving assistant is a genuinely differentiated spot in the map — with an 18–30-month window before Apple's Siri V2 makes "good enough" table stakes for everyone else.

---

## 7. Sources

**Laya / decision models**
- laya-browser-agent (local Laya runtime, MLX/CoreML backends, v3 element format) — github.com/ChenneyZhuang/laya-browser-agent
- "Laya: The Open-Source Jev Alternative, Benchmarked Honestly" (32.8 ms, ECE 0.081, zero-shot 0.362, fine-tuned 0.766, >20-option degradation) — flowtivity.ai/blog/laya-open-source-jev-alternative
- "Laya: AI Decision Engine at 32.8 ms" (Convai, Apache-2.0, checkpoints) — elsolitario.org
- AGTP benchmarks (laya-mlx <1 GB on MacBook Air; CoreML 3.7 ms on M5 Pro) — x.com/AGTPinsights
- "Laya AI review" — eesel.ai/blog/laya-ai-review

**Competitors**
- Fazm (macOS, MIT, voice, Accessibility) — github.com/mediar-ai/fazm; fazm.ai/blog
- Open-source computer-use agent survey (UI-TARS, Agent S3, CUA, OpenAdapt, self-operating-computer star counts/licenses) — turingpost.com; github AIHawk wiki
- Agent! for macOS 26 (Accessibility tools, voice/iMessage, local models) — github.com/macOS26/Agent
- macOS AX-tree perception (~50 ms, privacy) — fazm.ai/blog/macos-ai-agent

**Market**
- AI in Voice Assistants Market ($6.13B→$18.16B, 31.2% CAGR) — researchandmarkets.com
- Voice Assistant Application Market ($9.62B→$30.42B, 33.3% CAGR) — researchandmarkets.com
- Voice AI market segments ($3.51B 2026, Grand View; $47.5B 2034, Market.us) — voiceaiwrapper.com
- Voice assistant statistics (8.4B devices; 157M US users) — thestacc.com
- Apple Intelligence usage (940M devices, 410M DAU) — presenc.ai
- Mac record quarter ($10.4B, +29% YoY) — tech-insider.org
- Mac AI app adoption (79% of Mac users) + pricing benchmarks — localchat.app
- macOS 26 on 86% of Macs — aboutchromebooks.com/TelemetryDeck

**Siri gap**
- Personalized Siri ~18 months late, reliability problems — valueaddvc.com
- Siri V2 slipped from iOS 26.4 into iOS 27; Gemini partnership — mactech.com / Gurman; macworld.com

**Offline stack**
- whisper.cpp on Apple Silicon (Metal/CoreML/ANE, RAM/latency tables) — vexascribe.com; codersera.com; tokrepo.com
- Local voice assistant builds (1–1.5 s end-to-end on Mac Mini M5) — promptquorum.com
- Voice chat VRAM budgets (Kokoro 0.5 GB <300 ms; Piper <100 ms CPU) — insiderllm.com
- Custom wake words: openWakeWord (Apache-2.0, user-trainable); Porcupine free tier discontinued 2026-06-30 — openwakeword.com; violawake.com; github.com/nibor1896/custom-wakeword-trainer
- On-device LoRA/QLoRA on MLX (7B QLoRA ~7 GB, ~90 min/5k examples; adapters hot-swappable) — llmcheck.net; buildmvpfast.com; digitalapplied.com; codersera.com; blakecrosley.com
