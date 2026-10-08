import AppKit
import ApplicationServices
import AVFoundation
import AuraCore
import CoreGraphics

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

    /// Whether macOS currently trusts Aura for Accessibility — read live.
    ///
    /// `AXIsProcessTrusted()` alone is not enough for a menu-bar app that runs
    /// for days. HIServices answers it from a per-process cache that is only
    /// refreshed by the `com.apple.accessibility.api` distributed notification,
    /// and that notification can arrive *before* tccd has committed the grant —
    /// so the cache refills with the old "denied" and never corrects itself.
    /// The result is the exact complaint this app must never produce: the user
    /// has switched Aura on, System Settings agrees, and Aura still says no.
    ///
    /// An event tap cannot be created without the trust and cannot be answered
    /// from that cache, so it is the live probe. `AXIsProcessTrusted()` remains
    /// the fallback — if the probe is unavailable for any reason, this degrades
    /// to exactly the behaviour it replaces, never to a wrong "no".
    static var accessibilityGranted: Bool {
        if eventTapProbeSucceeds() { return true }
        return AXIsProcessTrusted()
    }

    /// A listen-only tap on the session's event stream: permitted only with the
    /// Accessibility grant. Created and torn down immediately — nothing is
    /// observed, and no prompt is raised when it is refused.
    private static func eventTapProbeSucceeds() -> Bool {
        let mask = CGEventMask(1 << CGEventType.mouseMoved.rawValue)
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            // The tap is never enabled, so this is never called; passing the
            // event back unretained keeps it correct even if that changes.
            callback: { _, _, event, _ in Unmanaged.passUnretained(event) },
            userInfo: nil
        ) else { return false }
        CFMachPortInvalidate(tap)
        return true
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
        // Reading System Events' version is a read-only AppleEvent. Avoid
        // enumerating application processes here: that also exercises AX and
        // can make an Automation test look like an Accessibility failure.
        let source = #"tell application id "com.apple.systemevents" to get version"#
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
                    ? "macOS needs permission to let Aura send Apple events to System Events. Turn Aura on in Privacy & Security › Automation."
                    : "The Automation test failed: \(detail)",
                permissionRequired: needsPermission)
        }

        let version = result.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let version, !version.isEmpty {
            return AutomationTestResult(
                ok: true,
                message: "Automation works — Aura reached System Events (version \(version)).",
                permissionRequired: false)
        }
        return AutomationTestResult(ok: false,
                                    message: "System Events didn't return a version, so Aura couldn't verify Automation access.",
                                    permissionRequired: false)
    }

    private static func open(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }
}
