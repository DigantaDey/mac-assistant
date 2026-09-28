import AppKit
import ApplicationServices
import AVFoundation

// The app owns the consent conversation. macOS attributes microphone and
// Accessibility grants to the *responsible* process — for the engine, that's
// Aura.app — so asking from here (once, on first launch) is what makes the
// TCC dialogs appear under the right name, with our usage strings.
enum Permissions {

    // MARK: Microphone

    static var microphoneStatus: AVAuthorizationStatus {
        AVCaptureDevice.authorizationStatus(for: .audio)
    }

    /// Returns true when the user granted access (or it was already granted).
    /// No-op — never prompts — when the status is already decided.
    @discardableResult
    static func requestMicrophone() async -> Bool {
        switch microphoneStatus {
        case .authorized:
            return true
        case .notDetermined:
            return await withCheckedContinuation { cont in
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    DispatchQueue.main.async { cont.resume(returning: granted) }
                }
            }
        case .denied, .restricted:
            return false
        @unknown default:
            return false
        }
    }

    /// Opens the exact pane for a revoked/denied permission.
    static func openMicrophoneSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: Accessibility

    static var accessibilityGranted: Bool {
        AXIsProcessTrusted()
    }

    /// Shows the system dialog ("Aura would like to control this computer…")
    /// with an Open System Settings button. macOS only shows it once per
    /// app; afterwards the user flips the toggle in System Settings.
    static func requestAccessibility() {
        guard !accessibilityGranted else { return }
        // kCFBooleanTrue, not a Swift Bool: the TCC API wants a CFBoolean.
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let options = [key: kCFBooleanTrue!] as CFDictionary
        AXIsProcessTrustedWithOptions(options)
    }

    static func openAccessibilitySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: Automation (kinds of)

    /// Automation (Apple Events) can't be asked for in advance — macOS asks
    /// per target app on first use. The engine's harmless probe is how the
    /// user sees that dialog; this just opens the pane for the rest.
    static func openAutomationSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation") {
            NSWorkspace.shared.open(url)
        }
    }
}
