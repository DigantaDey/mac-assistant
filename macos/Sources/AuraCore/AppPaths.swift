import Foundation

/// Every place Aura keeps something, in one file — so "where is my data?"
/// always has the same answer, and the app never guesses.
public enum AppPaths {

    /// ~/Library/Application Support/Aura
    public static var supportDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("Aura", isDirectory: true)
    }

    /// The engine copy the installer places here (a full checkout + venv).
    public static var installedEngineDirectory: URL {
        supportDirectory.appendingPathComponent("engine", isDirectory: true)
    }

    /// The shared secret both the app and the engine use (mode 0600).
    public static var tokenURL: URL {
        supportDirectory.appendingPathComponent("token", isDirectory: false)
    }

    /// The user's editable settings.
    public static var userConfigURL: URL {
        supportDirectory.appendingPathComponent("config.toml", isDirectory: false)
    }

    /// History, preferences and learned wake words.
    public static var dataDirectory: URL {
        supportDirectory
    }

    /// ~/Library/Logs/Aura.log — what the engine prints, plus app messages.
    public static var logURL: URL {
        let logs = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library")
        return logs.appendingPathComponent("Logs", isDirectory: true)
            .appendingPathComponent("Aura.log", isDirectory: false)
    }

    @discardableResult
    public static func ensureSupportDirectory() -> Bool {
        var ok = true
        for directory in [supportDirectory, supportDirectory.appendingPathComponent("models", isDirectory: true)] {
            do {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            } catch {
                ok = false
            }
        }
        return ok
    }

    /// Does this folder look like an Aura engine checkout?
    public static func looksLikeEngine(_ path: String) -> Bool {
        // The directory flag belongs to the *root*, not to the file inside it —
        // asking about `aura/__main__.py` made this always return false, which
        // silently broke engine discovery. (The unit test caught it.)
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
              isDirectory.boolValue else { return false }
        let hasPackage = FileManager.default.fileExists(
            atPath: (path as NSString).appendingPathComponent("aura/__main__.py"))
        let hasProject = FileManager.default.fileExists(
            atPath: (path as NSString).appendingPathComponent("pyproject.toml"))
        return hasPackage && hasProject
    }

    /// A human-readable path for display (abbreviates the home directory).
    public static func pretty(_ path: String) -> String {
        let home = NSHomeDirectory()
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }
}
