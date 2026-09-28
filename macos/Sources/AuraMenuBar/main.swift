import AppKit

// Aura — menu-bar shell.
//
// Spawns and babysits the Python orchestrator, presents the product UI in a
// WKWebView popover, owns the global ⌥Space hotkey, and optionally registers
// itself as a login item. Deliberately thin: all product logic lives in the
// Python engine; this layer only hosts, launches, and reflects.

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)   // menu-bar only — no Dock icon
application.run()
