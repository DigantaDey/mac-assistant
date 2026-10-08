import Foundation

/// The global shortcut that wakes Aura.
///
/// Raw key codes and Carbon modifier masks live here (rather than in the
/// Carbon-importing app layer) so the value can be stored, compared and tested
/// without a window server. The numbers are Apple's, and stable since forever:
/// space = 49, ⇧ = 512, ⌘ = 256, ⌥ = 2048, ⌃ = 4096.
public enum WakeShortcut: String, CaseIterable, Sendable {
    case optionSpace
    case controlSpace
    case commandShiftSpace
    case controlOptionSpace
    case none

    public var keyCode: UInt32? {
        switch self {
        case .none: return nil
        default: return 49   // kVK_Space
        }
    }

    public var modifiers: UInt32 {
        switch self {
        case .optionSpace: return 2048
        case .controlSpace: return 4096
        case .commandShiftSpace: return 256 | 512
        case .controlOptionSpace: return 4096 | 2048
        case .none: return 0
        }
    }

    public var label: String {
        switch self {
        case .optionSpace: return "⌥ Space"
        case .controlSpace: return "⌃ Space"
        case .commandShiftSpace: return "⇧ ⌘ Space"
        case .controlOptionSpace: return "⌃ ⌥ Space"
        case .none: return "None"
        }
    }

    public var detail: String {
        switch self {
        case .none: return "No global shortcut — open Aura from the menu bar."
        default: return "Works in any app, no permission needed."
        }
    }
}

/// Everything the app remembers between launches.
public enum Prefs {

    private static let defaults = UserDefaults.standard

    private enum Key {
        static let port = "port"
        static let enginePath = "repoPath"
        static let hasOnboarded = "hasOnboarded"
        static let shortcut = "wakeShortcut"
        static let lastVersion = "lastRunVersion"
        static let showPanelOnWake = "showPanelOnWake"
        static let speakReplies = "speakReplies"
        static let confirmHintSeen = "confirmHintSeen"
    }

    public static var port: Int {
        get {
            let value = defaults.integer(forKey: Key.port)
            return value > 0 ? value : 7331
        }
        set { defaults.set(newValue, forKey: Key.port) }
    }

    /// The engine folder the user chose (or the installer's copy).
    public static var enginePath: String? {
        get { defaults.string(forKey: Key.enginePath) }
        set { defaults.set(newValue, forKey: Key.enginePath) }
    }

    public static var hasOnboarded: Bool {
        get { defaults.bool(forKey: Key.hasOnboarded) }
        set { defaults.set(newValue, forKey: Key.hasOnboarded) }
    }

    public static var wakeShortcut: WakeShortcut {
        get {
            guard let raw = defaults.string(forKey: Key.shortcut),
                  let value = WakeShortcut(rawValue: raw) else { return .optionSpace }
            return value
        }
        set { defaults.set(newValue.rawValue, forKey: Key.shortcut) }
    }

    public static var lastRunVersion: String? {
        get { defaults.string(forKey: Key.lastVersion) }
        set { defaults.set(newValue, forKey: Key.lastVersion) }
    }

    /// Open the panel when the shortcut fires (vs. just listening).
    public static var showPanelOnWake: Bool {
        get { (defaults.object(forKey: Key.showPanelOnWake) as? Bool) ?? true }
        set { defaults.set(newValue, forKey: Key.showPanelOnWake) }
    }

    public static var confirmHintSeen: Bool {
        get { defaults.bool(forKey: Key.confirmHintSeen) }
        set { defaults.set(newValue, forKey: Key.confirmHintSeen) }
    }

    public static func reset() {
        for key in [Key.port, Key.enginePath, Key.hasOnboarded, Key.shortcut,
                    Key.lastVersion, Key.showPanelOnWake, Key.confirmHintSeen] {
            defaults.removeObject(forKey: key)
        }
    }
}

public enum AuraVersion {
    /// Kept in step with pyproject.toml and the bundle's CFBundleShortVersionString.
    public static let semantic = "0.7.2"
    public static var display: String { "Aura \(semantic)" }
}
