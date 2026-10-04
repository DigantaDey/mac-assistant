import AppKit
import ApplicationServices
import AVFoundation
import AuraCore

/// The app owns the consent conversation.
///
/// macOS attributes a microphone or Accessibility grant to the *responsible*
/// process — for Aura's engine, that is Aura.app — so the dialogs are asked
/// for here, once, and the engine's honest checks report the result.
struct AutomationTestResult: Sendable {
    let ok: Bool
    let message: String
    let permissionRequired: Bool
}

enum Permissions {

    // MARK: microphone

    static var microphoneStatus: AVAuthorizationStatus {
        AVCaptureDevice.authorizationStatus(for: .audio)
    }

    /// Prompts only when macOS hasn't decided yet; never nags.
    @discardableResult
    static func requestMicrophone() async -> Bool {
        switch microphoneStatus {
        case .authorized:
            return true
        case .notDetermined:
            return await withCheckedContinuation { continuation in
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    DispatchQueue.main.async { continuation.resume(returning: granted) }
                }
            }
        default:
            return false
        }
    }

    static func openMicrophoneSettings() {
        open("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
    }

    // MARK: accessibility

    static var accessibilityGranted: Bool {
        AXIsProcessTrusted()
    }

    /// Shows the system dialog once; afterwards the user flips the switch in
    /// System Settings (Aura opens the exact pane for them).
    static func requestAccessibility() {
        guard !accessibilityGranted else { return }
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let options = [key: kCFBooleanTrue as Any] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
    }

    static func openAccessibilitySettings() {
        open("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
    }

    // MARK: automation

    static func openAutomationSettings() {
        open("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")
    }

    /// Run the harmless AppleEvent from Aura.app itself. The engine's Python
    /// subprocess is the wrong identity to test TCC Automation with: depending
    /// on macOS, the consent prompt can be attributed to osascript/python and
    /// never appear beside Aura in Privacy & Security.
    static func testAutomation() -> AutomationTestResult {
        let source = #"tell application "System Events" to get name of first application process"#
        guard let script = NSAppleScript(source: source) else {
            return AutomationTestResult(ok: false,
                                        message: "Couldn't prepare the Automation test.",
                                        permissionRequired: false)
        }

        var error: NSDictionary?
        let result = script.executeAndReturnError(&error)
        if let error {
            let number = (error["NSAppleScriptErrorNumber"] as? NSNumber)?.intValue
            let detail = (error["NSAppleScriptErrorMessage"] as? String)
                ?? error.description
            let needsPermission = number == -1743
                || detail.localizedCaseInsensitiveContains("not authorized")
                || detail.localizedCaseInsensitiveContains("not allowed")
            return AutomationTestResult(
                ok: false,
                message: needsPermission
                    ? "macOS needs permission to let Aura control System Events. Turn Aura on in Privacy & Security › Automation."
                    : "The Automation test failed: \(detail)",
                permissionRequired: needsPermission)
        }

        if let reached = result.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines),
           !reached.isEmpty {
            return AutomationTestResult(
                ok: true,
                message: "Automation works — Aura reached System Events (\(reached)).",
                permissionRequired: false)
        }
        return AutomationTestResult(ok: true,
                                    message: "Automation works — Aura reached System Events.",
                                    permissionRequired: false)
    }

    private static func open(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }
}
