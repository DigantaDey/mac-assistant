import AppKit
import ApplicationServices
import AVFoundation
import AuraCore

/// The app owns the consent conversation.
///
/// macOS attributes a microphone or Accessibility grant to the *responsible*
/// process — for Aura's engine, that is Aura.app — so the dialogs are asked
/// for here, once, and the engine's honest checks report the result.
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

    private static func open(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }
}
