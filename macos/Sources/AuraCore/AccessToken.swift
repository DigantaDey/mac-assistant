import Foundation

/// The shared secret between the app and its engine.
///
/// The engine refuses every request that doesn't carry it, which is what stops
/// a random web page (or another process) from driving Aura through its
/// loopback port. The app generates it, stores it 0600, and hands it to the
/// child process through `AURA_TOKEN`.
public enum AccessToken {

    public static func read() -> String? {
        guard let data = try? Data(contentsOf: AppPaths.tokenURL),
              let value = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty
        else { return nil }
        return value
    }

    /// Load the token, creating one on first run. Never throws: if the disk
    /// refuses, the caller gets a usable in-memory token and Aura still works
    /// for this session.
    @discardableResult
    public static func loadOrCreate() -> String {
        if let existing = read() { return existing }
        let token = generate()
        try? write(token)
        return token
    }

    public static func write(_ token: String) throws {
        AppPaths.ensureSupportDirectory()
        let data = Data((token + "\n").utf8)
        try data.write(to: AppPaths.tokenURL, options: [.atomic])
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: AppPaths.tokenURL.path)
    }

    public static func generate() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        for index in bytes.indices {
            bytes[index] = UInt8.random(in: 0...255)
        }
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// True when the on-disk token is readable by others (a real warning worth
    /// surfacing in Settings rather than silently ignoring).
    public static func permissionsAreLoose() -> Bool {
        guard let attributes = try? FileManager.default
            .attributesOfItem(atPath: AppPaths.tokenURL.path),
              let mode = attributes[.posixPermissions] as? NSNumber
        else { return false }
        return mode.intValue & 0o077 != 0
    }
}
