import XCTest
@testable import AuraCore

/// Paths, tokens and shortcuts — the boring bits that, when wrong, make the app
/// look broken for reasons nobody can guess.
final class AppPathsTests: XCTestCase {

    func testLooksLikeAnEngineOnlyForRealCheckouts() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("aura-paths-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        XCTAssertFalse(AppPaths.looksLikeEngine(root.path))

        let auraDirectory = root.appendingPathComponent("aura")
        try FileManager.default.createDirectory(at: auraDirectory, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: auraDirectory.appendingPathComponent("__main__.py").path,
                                       contents: Data())
        // A package without pyproject.toml is not an installable engine.
        XCTAssertFalse(AppPaths.looksLikeEngine(root.path))

        FileManager.default.createFile(atPath: root.appendingPathComponent("pyproject.toml").path,
                                       contents: Data())
        XCTAssertTrue(AppPaths.looksLikeEngine(root.path))
    }

    func testPrettyPathsAbbreviateHome() {
        let inside = AppPaths.pretty(NSHomeDirectory() + "/Library/Logs/Aura.log")
        XCTAssertTrue(inside.hasPrefix("~"))
        XCTAssertEqual(AppPaths.pretty("/Applications/Aura.app"), "/Applications/Aura.app")
    }

    func testEveryPathLivesUnderOurOwnFolders() {
        let paths = [AppPaths.supportDirectory.path, AppPaths.logURL.path,
                     AppPaths.tokenURL.path, AppPaths.installedEngineDirectory.path]
        XCTAssertTrue(paths.allSatisfy { $0.contains("Aura") })
        XCTAssertTrue(AppPaths.tokenURL.path.hasSuffix("/Aura/token"))
    }
}

final class AccessTokenTests: XCTestCase {

    func testGeneratedTokensAreUnpredictableAndURLSafe() {
        let tokens = Set((0..<50).map { _ in AccessToken.generate() })
        XCTAssertEqual(tokens.count, 50, "tokens must not repeat")
        XCTAssertTrue(tokens.allSatisfy { $0.count >= 40 })
        XCTAssertTrue(tokens.allSatisfy { token in
            token.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
        })
    }
}

final class WakeShortcutTests: XCTestCase {

    func testShortcutsMapToRealKeyCodes() {
        for shortcut in WakeShortcut.allCases where shortcut != .none {
            XCTAssertEqual(shortcut.keyCode, 49, "space")
            XCTAssertNotEqual(shortcut.modifiers, 0, "a shortcut needs a modifier")
        }
        XCTAssertNil(WakeShortcut.none.keyCode)
        XCTAssertEqual(WakeShortcut.optionSpace.modifiers, 2048)
        XCTAssertEqual(WakeShortcut.controlSpace.modifiers, 4096)
        XCTAssertEqual(WakeShortcut.commandShiftSpace.modifiers, 256 | 512)
    }

    func testEveryShortcutHasALabel() {
        XCTAssertTrue(WakeShortcut.allCases.allSatisfy { !$0.label.isEmpty })
    }
}
