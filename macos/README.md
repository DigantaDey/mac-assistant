# Aura.app — the native macOS app

Aura is a **menu-bar app** (AppKit + SwiftUI) with a local Python engine
behind it. There is no browser UI and no WebKit anywhere: CI fails the build
on purpose if `import WebKit` or `WKWebView` appears under `Sources/`.

```
┌─ Aura.app (LSUIElement — no Dock icon) ───────────────────────────────────┐
│                                                                           │
│  ◉ NSStatusItem ─── left-click: NSPopover ▸ SwiftUI panel                 │
│  │   (icon mirrors live state)   orb · transcript · reply · confirm card  │
│  │   ● ready   ◉ working   ▲ needs your OK   ⚠ attention needed            │
│  └─ right-click: menu                                                     │
│        Open Aura (⌘O) · Wake Aura · Settings… (⌘,)                        │
│        Setup & Permissions… (⌘S) · Activity… (⌘A)                         │
│        Restart Engine (⌘R) · Choose Engine Folder… · Start at Login       │
│        Open Data Folder · Open Log (⌘L) · Quit Aura (⌘Q)                  │
│                                                                           │
│  Settings window — eight live sections: General · Voice · Understanding   │
│  · Safety · Permissions · Wake Phrase · Activity · About                  │
│                                                                           │
│  Onboarding (first launch, once) — explains what stays on this Mac       │
│  before asking for Microphone and Accessibility; the app owns both        │
│  dialogs, and the engine inherits the grants as its child.                │
│                                                                           │
│  The engine is still local: `.venv/bin/python -m aura serve` on           │
│  127.0.0.1:7331, token-protected, JSON only. The panel is native UI       │
│  talking to it — no page, no URL, no readiness gate.                      │
└───────────────────────────────────────────────────────────────────────────┘
```

## Inside the package

| Path | What it is |
|---|---|
| `Sources/AuraCore` | Foundation only: `EngineClient` (typed async client + self-healing SSE stream), `EngineSupervisor` (finds, adopts or reclaims the Python engine; bounded backoff restarts; no orphan child on quit), `AppPaths`, `AccessToken`, `AuraLog`, `Prefs`, and the typed models of the engine's JSON. Compiles and unit-tests in seconds. |
| `Sources/AuraMenuBar` | The product: `AppDelegate` (status item, popover, menu, windows), `AppModel` (the single object the UI binds to), `PanelView`, `SettingsView`, `OnboardingView`, `HotKey` (Carbon global ⌥Space + `SMAppService` login item), `Permissions`, `Theme`, `OrbView`. |
| `Tests/AuraCoreTests` | `swift test` — JSON value handling, the SSE parser, the client (token header on every request, 401 ⇒ “another engine holds this port”, 403 ⇒ the engine's own message, unreachable), paths, tokens and the shortcut map. |

## Build

The Xcode command line tools are enough — there is no Xcode project.

```bash
xcode-select --install             # once
./scripts/make_app.sh              # swift build -c release → macos/build/Aura.app
./scripts/make_app.sh --install    # → /Applications/Aura.app
```

`./scripts/install.sh` does the whole journey (Homebrew dependencies, models,
engine venv, the app, then opens it).

## First run

1. Aura appears in the menu bar and shows a short welcome: what it can do,
   and that speech, planning and memory stay on this Mac.
2. macOS asks for **Microphone** (audio never leaves the Mac) and
   **Accessibility** (so Aura can type and click for you). Declining is fine
   — the app offers the exact System Settings pane instead.
3. If you built from a clone and the engine isn't installed under
   `~/Library/Application Support/Aura/engine`, Aura asks you to pick the
   folder **once**; it remembers it (changeable in Settings → General).

## Daily use

| Gesture | Result |
|---|---|
| `⌥Space` anywhere | Wake Aura and show the panel (configurable) |
| Click `◉` | Show or hide the panel |
| Right-click `◉` | Wake · Settings · Restart engine · Choose engine folder · log · login · Quit |
| Icon | template = ready · accent = working · orange = needs your OK · red = attention |

## Where things live

- Data, config and the access token: `~/Library/Application Support/Aura/`
- Engine log (5 MB rotating, openable from the menu): `~/Library/Logs/Aura.log`
- Engine API: `http://127.0.0.1:7331` — loopback only, JSON only, no HTML.

## Privacy model

- Everything runs locally: speech, planning and actions happen on this Mac,
  and the only process Aura talks to is its own engine on loopback.
- The port is guarded: every request carries `X-Aura-Token` (a 0600 file
  generated on first launch), requests with a browser `Origin` or a
  non-loopback `Host` are refused, and no CORS headers are ever sent.
- No telemetry, no accounts, no cloud. Uninstalling is `rm` of the app plus
  the two folders above.

## Troubleshooting

- **The panel names the problem.** Engine missing, port taken by another
  program, permissions revoked — the panel says which, and what to do. The
  menu keeps working while it does.
- **The engine won't start** — open the log (menu → Open Log, or Settings →
  Activity). The supervisor retries with backoff, adopts an engine that is
  already running, and reclaims the port from a stale Aura process — but
  never touches a process that isn't Aura; it names the occupant instead.
- **Start over with defaults** — delete
  `~/Library/Application Support/Aura/config.toml`; shipped defaults are in
  the repo's `config.default.toml`.
