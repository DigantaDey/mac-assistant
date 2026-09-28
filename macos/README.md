# Aura menu-bar shell (native Swift)

A thin AppKit host that makes Aura feel like a real Mac app — while all
product logic stays in the Python engine you can read and test anywhere.

```
┌─ Aura.app (LSUIElement — menu-bar only, no Dock icon) ──────────────┐
│                                                                      │
│  ◉ NSStatusItem ──── left-click: NSPopover ▸ WKWebView               │
│   │  (icon reflects state)            loading http://127.0.0.1:7331  │
│   │  ● ready   ◉ busy   ▲ needs OK   ⚠ offline                       │
│   └─ right-click: menu                                               │
│        Open Aura · Wake Aura (⌥Space)                                │
│        Choose Aura Folder… · Start at Login · Activity Log · Quit    │
│                                                                      │
│  PythonProcess — spawns `.venv/bin/python -m aura serve`             │
│    · logs → ~/Library/Logs/Aura.log (5 MB rotating)                  │
│    · crash → restart after 3 s · quit → clean SIGTERM shutdown       │
│    · GUI-safe PATH (+ /opt/homebrew/bin) · AURA_DATA_DIR set         │
│                                                                      │
│  ServerMonitor — polls /api/health every 2 s → icon state            │
│  Carbon hotkey  — global ⌥Space (no permission required)             │
│  SMAppService   — Start at Login toggle                              │
└──────────────────────────────────────────────────────────────────────┘
```

## Build (no Xcode project needed)

```bash
# Xcode command line tools are enough:
xcode-select --install        # if you don't have swift already

./scripts/make_app.sh             # → build/Aura.app
./scripts/make_app.sh --install   # → /Applications/Aura.app
```

## First run

1. Double-click `Aura.app` — a `◉` appears in your menu bar.
2. When asked, **select your `mac-assistant` folder** (the one containing `aura/`).
   - If a `.venv` exists inside, it's used; otherwise Aura runs with system
     `python3` (demo mode — full UI, simulated actions).
3. The panel opens straight to **Setup** on first launch: grant Microphone,
   Accessibility, run the automation test, install the local models. The
   menu-bar icon turns from red → white as you go.

## Daily use

| Gesture | Result |
|---|---|
| `⌥Space` anywhere | Wake Aura + open the panel |
| Click `◉` | Open/close the panel (480 pt popover, compact UI) |
| Right-click `◉` | Menu (wake, folder, login, log, quit) |
| Icon colors | white = ready · blue = working · orange = needs your OK · red = offline |

Everything the shell shows comes from the same engine as the browser UI —
`http://127.0.0.1:7331` stays available in any browser too.

## Trust notes

- The app is **local-only**: it talks to `127.0.0.1` and spawns one child
  process. No telemetry, no network beyond what the engine does (nothing).
- TCC prompts (microphone, automation) are attributed to Aura.app via the
  child process, which is why the bundle carries
  `NSMicrophoneUsageDescription` / `NSAppleEventsUsageDescription`.
- Ad-hoc signed (`codesign -s -`). For distribution you'd replace this with a
  Developer ID signature + notarization — see ROADMAP v0.5.
