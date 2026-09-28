# Aura — the native shell

A thin AppKit host that makes Aura feel like a real Mac app — while all
product logic stays in the Python engine you can read and test anywhere.

```
┌─ Aura.app (LSUIElement — menu-bar app) ─────────────────────────────────┐
│                                                                          │
│  ◉ NSStatusItem ──── left-click: NSPopover ▸ WKWebView                   │
│   │  (icon mirrors live state)        loading http://127.0.0.1:7331      │
│   │  ● ready   ◉ busy   ▲ needs OK   ⚠ offline                           │
│   └─ right-click: menu                                                        │
│        Open Aura · Wake Aura (⌥Space) · Setup & Permissions…            │
│        Open in Browser · Restart Engine                                    │
│        Choose Aura Folder… · Start at Login · Activity Log · Quit        │
│                                                                          │
│  First launch — "Welcome to Aura" NSWindow (720 pt)                      │
│   · WKWebView loads the Setup wizard directly                            │
│   · the app itself asks macOS for Microphone (its dialog carries         │
│     NSMicrophoneUsageDescription) and shows the Accessibility prompt     │
│   · closes when the user is done; the menu-bar icon takes over           │
│                                                                          │
│  Server readiness gate — the webview shows a native "Waking Aura…"       │
│  placeholder until /api/health answers; never a blank or dead URL.       │
│                                                                          │
│  PythonProcess — spawns the engine:                                       │
│   · discovery:  UserDefaults folder → Application Support/Aura/engine    │
│     (the copy the installer places) → file picker as a last resort       │
│   · `.venv/bin/python -m aura serve`, AURA_ENGINE_PATH + AURA_DATA_DIR    │
│   · logs → ~/Library/Logs/Aura.log (5 MB rotating)                       │
│   · crash → restart after 3 s · menu → Restart Engine                    │
│   · GUI-safe PATH (+ /opt/homebrew/bin)                                  │
│                                                                          │
│  ServerMonitor — polls /api/health every 2 s → icon state                 │
│  Carbon hotkey  — global ⌥Space (no permission required)                 │
│  SMAppService   — Start at Login toggle                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

## Build (no Xcode project needed)

```bash
# Xcode command line tools are enough:
xcode-select --install        # if you don't have swift already

./scripts/make_app.sh             # → build/Aura.app
./scripts/make_app.sh --install   # → /Applications/Aura.app
```

Or skip both: `./scripts/install.sh` does everything, including this.

## First run

1. Double-click `Aura.app` — the **Welcome to Aura** window opens.
2. macOS asks for the **Microphone** (the dialog says audio never leaves
   your Mac) and for **Accessibility** (with an Open System Settings
   button). Grant both — or do it later any time from
   **Settings → Permissions** or the menu bar → *Setup & Permissions…*.
3. The Setup panel shows the honest state of each piece: microphone,
   accessibility, automation (test event), speech model, and Aura's mind.
   Each card has the exact action — enable, open the right System
   Settings pane, or download.

If you installed from a clone (not via `scripts/install.sh`), the app asks
you to pick the `mac-assistant` folder **once**; it remembers it.

## Daily use

| Gesture | Result |
|---|---|
| `⌥Space` anywhere | Wake Aura + open the panel |
| Click `◉` | Open/close the panel (480 pt popover, compact UI) |
| Right-click `◉` | Menu (wake, setup, browser, restart, login, log, quit) |
| Icon colors | white = ready · blue = working · orange = needs your OK · red = offline |

## Trust notes

- The app is **local-only**: it talks to `127.0.0.1` and spawns one child
  process. No telemetry, no network beyond what the engine does (nothing).
- TCC prompts are attributed to Aura.app because the app owns the consent
  conversation (AVFoundation mic request + `AXIsProcessTrustedWithOptions`),
  and the engine is its child — which is why the bundle carries
  `NSMicrophoneUsageDescription` / `NSAppleEventsUsageDescription`.
- Ad-hoc signed (`codesign -s -`). For distribution you'd replace this with
  a Developer ID signature + notarization — see ROADMAP.
