import AppKit
import AuraCore
import Carbon.HIToolbox
import ServiceManagement
import SwiftUI

/// A real global shortcut (⌥Space by default).
///
/// Carbon's `RegisterEventHotKey` is still the right tool in 2026: it works
/// while Aura is in the background, needs no Accessibility permission, and
/// can't be swallowed by a focused app. (The old shell called
/// `InstallApplicationEventHandler`, a C macro Swift cannot import — that is
/// the bug this file fixes.)
final class GlobalHotKey {

    private var hotKeyRef: EventHotKeyRef?
    private var eventHandler: EventHandlerRef?
    private let action: () -> Void

    private static var nextID: UInt32 = 1

    init?(shortcut: WakeShortcut, action: @escaping () -> Void) {
        self.action = action
        guard let keyCode = shortcut.keyCode else { return nil }
        register(keyCode: keyCode, modifiers: shortcut.modifiers)
    }

    deinit {
        unregister()
    }

    func unregister() {
        if let hotKeyRef {
            _ = UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let eventHandler {
            _ = RemoveEventHandler(eventHandler)
            self.eventHandler = nil
        }
    }

    private func register(keyCode: UInt32, modifiers: UInt32) {
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))

        let selfPointer = Unmanaged.passUnretained(self).toOpaque()
        _ = InstallEventHandler(GetApplicationEventTarget(), { _, event, userData in
            guard let event, let userData else { return noErr }
            guard GetEventKind(event) == UInt32(kEventHotKeyPressed) else { return noErr }
            let hotKey = Unmanaged<GlobalHotKey>.fromOpaque(userData).takeUnretainedValue()
            DispatchQueue.main.async { hotKey.action() }
            return noErr
        }, 1, &eventType, selfPointer, &eventHandler)

        let id = EventHotKeyID(signature: GlobalHotKey.fourCC("AURA"), id: GlobalHotKey.nextID)
        GlobalHotKey.nextID += 1
        let status = RegisterEventHotKey(keyCode, modifiers, id,
                                         GetApplicationEventTarget(), 0, &hotKeyRef)
        if status != noErr {
            NSLog("Aura: couldn't register the global shortcut (status \(status))")
        }
    }

    private static func fourCC(_ text: String) -> OSType {
        var value: OSType = 0
        for byte in text.utf8.prefix(4) { value = (value << 8) | OSType(byte) }
        return value
    }
}

/// "Start at Login" — SMAppService, with an honest error path.
enum LaunchAtLogin {

    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    /// Returns a message when macOS refused, so the UI can say why.
    static func set(enabled: Bool) -> String? {
        do {
            if enabled {
                if SMAppService.mainApp.status != .enabled { try SMAppService.mainApp.register() }
            } else {
                if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            }
            return nil
        } catch {
            let hint = enabled
                ? "macOS may need you to approve Aura in System Settings › General › Login Items."
                : "Aura couldn't remove itself from your login items."
            return "\(hint) (\(error.localizedDescription))"
        }
    }
}
