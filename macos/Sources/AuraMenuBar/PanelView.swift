import AuraCore
import SwiftUI

/// Aura's main surface: what's happening, what she's about to do, and one
/// field to type into. Everything here is native SwiftUI.
struct PanelView: View {
    @EnvironmentObject var model: AppModel
    @State private var draft = ""
    @FocusState private var composerFocused: Bool

    private let suggestions = [
        "What's on my screen?",
        "Open Spotify",
        "Empty the trash",
        "Set volume to 30",
    ]

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if let error = model.lastError, !model.engineStatus.isRunning {
                            engineProblemCard(error)
                        }
                        if model.isDemoProfile {
                            demoBanner
                        } else if !model.micReady {
                            microphoneBanner
                        } else if model.state?.plannerOnline == false {
                            brainBanner
                        }

                        if model.messages.isEmpty {
                            welcome
                        } else {
                            ForEach(model.messages) { message in
                                MessageRow(message: message)
                            }
                        }

                        if !model.liveActions.isEmpty {
                            LiveActionsView(actions: model.liveActions)
                        }

                        if let proposal = model.proposal {
                            ProposalCard(proposal: proposal,
                                         onRun: { model.confirmProposal() },
                                         onCancel: { model.cancelProposal() })
                        }

                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(14)
                }
                .onChange(of: model.messages.count) { _ in
                    withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onChange(of: model.proposal?.id) { _ in
                    withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) }
                }
            }

            Divider()
            composer
        }
        .frame(width: Theme.panelWidth, height: Theme.panelHeight)
        .background(.ultraThinMaterial)
        .overlay(alignment: .bottom) {
            if let toast = model.toast {
                ToastView(toast: toast).padding(.bottom, 84)
            }
        }
        .onAppear {
            model.loadLog()
            Task { await model.refreshAll() }
        }
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 10) {
            StatusDot(color: Theme.color(for: model.phase),
                      size: 10,
                      pulsing: model.isBusy || model.awaitingConfirmation)

            VStack(alignment: .leading, spacing: 1) {
                Text("Aura")
                    .font(.system(size: 13, weight: .semibold))
                Text(model.phaseLabel)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer()

            if model.state?.plannerOnline == false && !model.isDemoProfile {
                Badge(text: "Basic mode", color: Theme.attention, icon: "bolt.slash")
            }
            if model.isDemoProfile {
                Badge(text: "Developer", color: Theme.busy, icon: "hammer")
            }

            Button {
                model.wake()
            } label: {
                Label("Wake", systemImage: "mic.fill")
                    .font(.system(size: 11, weight: .semibold))
            }
            .auraButton()
            .help("Ask Aura to listen now")
            .disabled(model.isBusy)

            Button {
                NotificationCenter.default.post(name: .auraOpenPanel, object: "settings")
            } label: {
                Image(systemName: "gearshape.fill").font(.system(size: 12))
            }
            .auraButton()
            .help("Settings")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    // MARK: banners

    private func engineProblemCard(_ message: String) -> some View {
        Card {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(Theme.down)
                Text("Aura's engine needs attention")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
            }
            Text(message)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Restart Engine") { model.restartEngine() }.auraButton(prominent: true)
                Button("Choose Folder…") { model.chooseEngineFolder() }.auraButton()
                Button("Open Log") { model.openLogFolder() }.auraButton()
            }
        }
    }

    private var demoBanner: some View {
        Card(spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: "hammer.fill").font(.system(size: 10))
                Text("Developer build").font(.system(size: 11, weight: .semibold))
            }
            .foregroundStyle(Theme.busy)
            Text("Actions are simulated on this machine — nothing will be changed.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
    }

    private var microphoneBanner: some View {
        Card(spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "mic.slash.fill").font(.system(size: 10))
                Text("Aura can't hear you yet").font(.system(size: 11, weight: .semibold))
            }
            .foregroundStyle(Theme.attention)
            HStack(spacing: 8) {
                Button("Allow microphone") { model.requestPermission("microphone") }.auraButton(prominent: true)
                Button("Open Setup") {
                    NotificationCenter.default.post(name: .auraOpenPanel, object: "settings/setup")
                }.auraButton()
            }
        }
    }

    private var brainBanner: some View {
        Card(spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "brain.head.profile").font(.system(size: 11))
                Text("Running on built-in skills").font(.system(size: 11, weight: .semibold))
            }
            .foregroundStyle(Theme.attention)
            Text("Your local language model isn't answering, so Aura uses her built-in commands. Everything still works, just more literally.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Button("See Setup") {
                NotificationCenter.default.post(name: .auraOpenPanel, object: "settings/setup")
            }
            .auraButton()
        }
    }

    private var welcome: some View {
        VStack(spacing: 12) {
            OrbView(phase: model.phase, diameter: 128) { model.wake() }
                .padding(.top, 6)
                .disabled(model.isBusy)

            VStack(spacing: 3) {
                Text(model.phase == "armed" ? "Say your wake phrase" : model.phaseLabel)
                    .font(.system(size: 13, weight: .semibold))
                Text(model.isListening ? "…and just talk" : "or type a command below")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 4)
    }

    // MARK: composer

    private var composer: some View {
        VStack(spacing: 8) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(suggestions, id: \.self) { suggestion in
                        Button(suggestion) {
                            model.send(suggestion)
                            draft = ""
                        }
                        .auraButton()
                        .disabled(model.isBusy)
                    }
                }
                .padding(.horizontal, 14)
            }

            HStack(spacing: 8) {
                Image(systemName: "text.cursor")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)

                TextField("Type a command…", text: $draft)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .focused($composerFocused)
                    .onSubmit(submit)

                if !draft.isEmpty {
                    Button {
                        draft = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .help("Clear")
                }

                Button(action: submit) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 18))
                        .foregroundStyle(draft.isEmpty ? Color.secondary : Theme.busy)
                }
                .buttonStyle(.plain)
                .disabled(draft.isEmpty)
                .help("Send")
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color.primary.opacity(0.06))
            )
            .padding(.horizontal, 12)

            HStack(spacing: 10) {
                Button(action: { model.clearConversation() }) {
                    Label("Clear", systemImage: "broom")
                }
                .auraButton()
                .help("Clear this conversation")

                Button(action: {
                    NotificationCenter.default.post(name: .auraOpenPanel, object: "activity")
                }) {
                    Label("Activity", systemImage: "clock.arrow.circlepath")
                }
                .auraButton()

                Button(action: { model.openDataFolder() }) {
                    Label("My data", systemImage: "folder")
                }
                .auraButton()

                Spacer()

                Text(AuraVersion.display)
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 10)
        }
        .padding(.top, 8)
    }

    private func submit() {
        let text = draft
        draft = ""
        model.send(text)
    }
}

// MARK: - conversation

struct MessageRow: View {
    let message: PanelMessage

    var body: some View {
        switch message.role {
        case .you:
            HStack {
                Spacer(minLength: 40)
                Text(message.text)
                    .font(.system(size: 12.5))
                    .padding(.horizontal, 11)
                    .padding(.vertical, 7)
                    .background(
                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .fill(Theme.busy.opacity(0.18))
                    )
                    .textSelection(.enabled)
            }
        case .aura:
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: message.failed ? "exclamationmark.circle.fill" : "sparkle")
                    .font(.system(size: 11))
                    .foregroundStyle(message.failed ? Theme.danger : Theme.busy)
                    .padding(.top, 3)
                Text(message.text)
                    .font(.system(size: 12.5))
                    .foregroundStyle(message.failed ? Theme.danger : .primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                Spacer(minLength: 0)
            }
        case .system:
            Text(message.text)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
                .multilineTextAlignment(.center)
        }
    }
}

struct LiveActionsView: View {
    let actions: [LiveAction]

    var body: some View {
        Card(spacing: 8) {
            SectionTitle(text: "Working", subtitle: nil)
            ForEach(actions) { action in
                HStack(spacing: 8) {
                    switch action.status {
                    case .running:
                        ProgressView().controlSize(.small).frame(width: 12, height: 12)
                    case .ok:
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 11)).foregroundStyle(Theme.ready)
                    case .failed:
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 11)).foregroundStyle(Theme.danger)
                    }
                    Text(action.title).font(.system(size: 12))
                    if let message = action.message, action.status != .running {
                        Text(message)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }
}

// MARK: - the confirmation card

struct ProposalCard: View {
    let proposal: Proposal
    var onRun: () -> Void
    var onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "shield.lefthalf.filled")
                    .foregroundStyle(Theme.attention)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Aura has a plan — \(proposal.actions.count) "
                         + (proposal.actions.count == 1 ? "action" : "actions"))
                        .font(.system(size: 12.5, weight: .semibold))
                    Text("Nothing happens until you say so.")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }

            if !proposal.reply.isEmpty {
                Text(proposal.reply)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 6) {
                ForEach(proposal.actions) { action in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: action.isBlocked ? "nosign" : (action.needsCare ? "exclamationmark.triangle.fill" : "checkmark.seal.fill"))
                            .font(.system(size: 11))
                            .foregroundStyle(Theme.color(forLevel: action.isBlocked ? "blocked" : (action.needsCare ? "confirm" : "safe")))
                            .padding(.top, 2)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(action.title).font(.system(size: 12, weight: .medium))
                            if !action.why.isEmpty {
                                Text(action.why)
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            if !action.reasons.isEmpty {
                                Text(action.reasons.joined(separator: " · "))
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(.tertiary)
                            }
                        }
                        Spacer(minLength: 0)
                        if action.needsCare {
                            Badge(text: action.risk, color: Theme.attention)
                        }
                    }
                }
            }

            HStack(spacing: 8) {
                Spacer()
                Button("Cancel", action: onCancel)
                    .auraButton()
                    .keyboardShortcut(.cancelAction)
                Button("Run it", action: onRun)
                    .auraButton(prominent: true, tint: Theme.attention)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                .fill(Theme.attention.opacity(0.10))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                .stroke(Theme.attention.opacity(0.35), lineWidth: 1)
        )
    }
}

// MARK: - toast

struct ToastView: View {
    let toast: Toast

    private var color: Color {
        switch toast.kind {
        case .info: return Theme.busy
        case .success: return Theme.ready
        case .warning: return Theme.attention
        case .failure: return Theme.danger
        }
    }

    var body: some View {
        Text(toast.text)
            .font(.system(size: 11.5, weight: .medium))
            .foregroundStyle(.white)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(Capsule().fill(color.opacity(0.95)))
            .shadow(color: .black.opacity(0.25), radius: 8, y: 3)
            .padding(.horizontal, 16)
            .transition(.move(edge: .bottom).combined(with: .opacity))
    }
}
