import AppKit
import AuraCore
import SwiftUI

/// First launch: a real window, a real conversation, three short steps.
/// Nothing is asked for that the user can't see the reason for.
struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    var onFinish: () -> Void

    @State private var step = 0
    @State private var microphoneGranted = false
    @State private var accessibilityGranted = false
    @State private var asked = false

    private let steps = ["Welcome", "Permissions", "Try it"]

    var body: some View {
        VStack(spacing: 0) {
            header

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch step {
                    case 0: welcome
                    case 1: permissions
                    default: tryIt
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            Divider()
            footer
        }
        .frame(width: 620, height: 540)
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            refresh()
            Task { await model.refreshPermissions() }
        }
        .onChange(of: model.nativeAccessibilityGranted) { value in
            if let value { accessibilityGranted = value }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await model.refreshPermissions(); refresh() }
        }
        .overlay(alignment: .bottom) {
            if let toast = model.toast {
                ToastView(toast: toast)
                    .padding(.bottom, 58)
                    .allowsHitTesting(false)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            OrbView(phase: model.phase, diameter: 40) {}
            VStack(alignment: .leading, spacing: 1) {
                Text("Welcome to Aura").font(.system(size: 15, weight: .semibold))
                Text("Your private assistant, running on this Mac.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            HStack(spacing: 6) {
                ForEach(Array(steps.enumerated()), id: \.offset) { index, title in
                    Text(title)
                        .font(.system(size: 10, weight: index == step ? .semibold : .regular))
                        .foregroundStyle(index == step ? Color.primary : Color.secondary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Capsule().fill(index == step ? Color.primary.opacity(0.10) : .clear))
                }
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

    // MARK: steps

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Aura listens, understands, and does the small things for you — opening apps, clicking buttons, filling forms, reading your screen.")
                .font(.system(size: 12.5))
                .fixedSize(horizontal: false, vertical: true)

            Card {
                SectionTitle(text: "What stays on this Mac", subtitle: nil)
                bullet("mic.fill", "Your voice is transcribed here — no audio is uploaded.")
                bullet("brain", "Requests are planned by a local model, not a cloud service.")
                bullet("hand.raised.fill", "Nothing destructive runs without your explicit yes.")
                bullet("eye.slash.fill", "No account, no telemetry, no analytics.")
            }

            Text("Next: two macOS permissions, each with one click.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }

    private var permissions: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Aura asks macOS for exactly two things — and you can revoke both at any time.")
                .font(.system(size: 12.5))
                .fixedSize(horizontal: false, vertical: true)

            Card {
                permissionRow(title: "Microphone",
                              detail: "To hear your wake phrase and your commands.",
                              granted: microphoneGranted,
                              actionTitle: microphoneGranted ? "Granted" : "Allow",
                              action: {
                                  Task {
                                      let granted = await Permissions.requestMicrophone()
                                      refresh()
                                      if granted {
                                          // A microphone grant doesn't replace
                                          // the engine's SilentMic stream; attach
                                          // the live input before wake setup.
                                          model.requestPermission("microphone")
                                      } else {
                                          model.toast("Allow Aura under System Settings › Privacy & Security › Microphone.",
                                                      kind: .warning)
                                          Permissions.openMicrophoneSettings()
                                      }
                                  }
                              },
                              settingsAction: Permissions.openMicrophoneSettings)
                Divider().opacity(0.4)
                permissionRow(title: "Accessibility",
                              detail: "So Aura can see and click inside your apps when you ask her to.",
                              granted: accessibilityGranted,
                              actionTitle: accessibilityGranted ? "Granted" : "Open System Settings",
                              action: {
                                  Permissions.requestAccessibility()
                                  Permissions.openAccessibilitySettings()
                              },
                              settingsAction: Permissions.openAccessibilitySettings)
            }

            Card {
                SectionTitle(text: "Automation (asked later, per app)",
                             subtitle: "The first time Aura drives Safari or Spotify, macOS asks you then — not now.")
                Button {
                    model.testAutomation()
                } label: {
                    HStack(spacing: 6) {
                        if model.isTestingAutomation { ProgressView().controlSize(.small) }
                        Text(model.isTestingAutomation ? "Testing…" : "Run a harmless test")
                    }
                }
                .auraButton()
                .disabled(model.isTestingAutomation)
            }

            Text("Still not sure? Skip it — Aura works with typing, and Settings ▸ Permissions shows the same switches later.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var tryIt: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("That's everything. Try one of these — or say “\(model.state?.wakePhrase?.isEmpty == false ? (model.state?.wakePhrase ?? "Hey Aura") : "Hey Aura")”.")
                .font(.system(size: 12.5))
                .fixedSize(horizontal: false, vertical: true)

            Card {
                SectionTitle(text: "Good first requests", subtitle: nil)
                ForEach(["What's on my screen?", "Open Spotify", "Set volume to 30", "What fields does this form have?"], id: \.self) { suggestion in
                    HStack(spacing: 8) {
                        Text(suggestion).font(.system(size: 12))
                        Spacer()
                        Button("Run") { model.send(suggestion) }.auraButton()
                    }
                }
            }

            Card {
                SectionTitle(text: "Where things live", subtitle: nil)
                InfoRow(label: "Shortcut", value: Prefs.wakeShortcut.label)
                InfoRow(label: "Menu bar", value: "Click the orb for this panel; right-click for settings")
                InfoRow(label: "History", value: "Settings ▸ Activity keeps every session on this Mac")
            }
        }
    }

    // MARK: footer

    private var footer: some View {
        HStack(spacing: 10) {
            if step > 0 {
                Button("Back") { step -= 1 }.auraButton()
            }
            Spacer()
            Button(step == steps.count - 1 ? "Finish" : "Continue") {
                if step == steps.count - 1 {
                    Prefs.hasOnboarded = true
                    onFinish()
                } else {
                    step += 1
                    if step == 1 { askForPermissions() }
                }
            }
            .auraButton(prominent: true)
            .keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

    // MARK: helpers

    private func bullet(_ icon: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 11))
                .foregroundStyle(Theme.busy)
                .frame(width: 14)
            Text(text)
                .font(.system(size: 11.5))
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }

    private func permissionRow(title: String, detail: String, granted: Bool,
                              actionTitle: String, action: @escaping () -> Void,
                              settingsAction: @escaping () -> Void) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: granted ? "checkmark.circle.fill" : "circle.dashed")
                .font(.system(size: 13))
                .foregroundStyle(granted ? Theme.ready : Theme.attention)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 12.5, weight: .medium))
                Text(detail).font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            VStack(spacing: 4) {
                Button(actionTitle, action: action).auraButton(prominent: !granted)
                Button("Settings", action: settingsAction).auraButton()
            }
        }
    }

    /// The one moment Aura asks macOS directly — so the dialog carries Aura's
    /// own usage strings and nothing is asked twice.
    private func askForPermissions() {
        guard !asked else { return }
        asked = true
        Task {
            let microphoneAllowed = await Permissions.requestMicrophone()
            if microphoneAllowed {
                // The engine was launched before onboarding and may currently
                // hold SilentMic. Hot-attach its stream after the native grant.
                model.requestPermission("microphone")
            }
            try? await Task.sleep(nanoseconds: 900_000_000)
            Permissions.requestAccessibility()
            refresh()
        }
    }

    private func refresh() {
        microphoneGranted = Permissions.microphoneStatus == .authorized
        accessibilityGranted = model.accessibilityStatus
            ?? Permissions.accessibilityGranted
    }
}
