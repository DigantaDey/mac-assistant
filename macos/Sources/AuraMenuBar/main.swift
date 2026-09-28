import AppKit

// Aura — a native menu-bar app.
//
// No storyboard, no nib, no web view: an accessory-policy NSApplication (so
// there is no Dock icon) with one delegate that builds the menu-bar item, the
// SwiftUI popover, and the windows on top of the local engine.

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
