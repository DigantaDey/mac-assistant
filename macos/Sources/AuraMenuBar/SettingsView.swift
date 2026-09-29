import AppKit
import AuraCore
import SwiftUI

/// The Settings window: every switch Aura has, in plain language, with the
/// engine's own state as the source of truth.
struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var section: Section = .general

    enum Section: String, CaseIterable, Identifiable {
        case general, voice, brain, safety, permissions, wake, activity, about

        var id: String { rawValue }

        var title: String {
            switch self {
            case .general: return "General"
            case .voice: return "Voice"
            case .brain: return "Understanding"
            case .safety: return "Safety"
            case .permissions: return "Permissions"
            case .wake: return "Wake Phrase"
            case .activity: return "Activity"
            case .about: return "About"
            }
        }

        var icon: String {
            switch self {
            case .general: return "gearshape"
            case .voice: return "waveform"
            case .brain: return "brain.head.profile"
            case .safety: return "shield.lefthalf.filled"
            case .permissions: return "lock.shield"
            case .wake: return "mic.badge.plus"
            case .activity: return "clock.arrow.circlepath"
            case .about: return "info.circle"
            }
        }

        static func from(_ deepLink: String?) -> Section? {
            switch deepLink {
            case "settings/setup", "setup", "permissions": return .permissions
            case "activity": return .activity
            case "wake": return .wake
            case "voice": return .voice
            case "brain": return .brain
            case "safety": return .safety
            case "about": return .about
            case "settings", "general": return .general
            default: return nil
            }
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text(section.title)
                        .font(.system(size: 18, weight: .semibold))
                    content
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 760, minHeight: 580)
        .onAppear {
            if let requested = Section.from(model.requestedSection) { section = requested }
            model.requestedSection = nil
            Task { await model.refreshAll() }
        }
        .onChange(of: model.requestedSection) { value in
            if let requested = Section.from(value) { section = requested }
            model.requestedSection = nil
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                StatusDot(color: Theme.color(for: model.phase), size: 9)
                VStack(alignment: .leading, spacing: 0) {
                    Text("Aura").font(.system(size: 13, weight: .semibold))
                    Text(model.engineStatus.label)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal, 12)
            .padding(.top, 14)
            .padding(.bottom, 10)

            ForEach(Section.allCases) { item in
                Button {
                    section = item
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: item.icon)
                            .font(.system(size: 12))
                            .frame(width: 16)
                        Text(item.title).font(.system(size: 12, weight: section == item ? .semibold : .regular))
                        Spacer()
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(
                        RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .fill(section == item ? Color.primary.opacity(0.10) : Color.clear)
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(section == item ? Color.primary : Color.secondary)
                .padding(.horizontal, 6)
            }

            Spacer()

            VStack(alignment: .leading, spacing: 3) {
                Text("On-device")
                    .font(.system(size: 10, weight: .semibold))
                Text("No cloud. No account. Nothing leaves this Mac.")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
        }
        .frame(width: 180)
    }

    @ViewBuilder
    private var content: some View {
        switch section {
        case .general: GeneralSection()
        case .voice: VoiceSection()
        case .brain: BrainSection()
        case .safety: SafetySection()
        case .permissions: PermissionsSection()
        case .wake: WakeSection()
        case .activity: ActivitySection()
        case .about: AboutSection()
        }
    }
}

// MARK: - General

private struct GeneralSection: View {
    @EnvironmentObject var model: AppModel
    @State private var shortcut = Prefs.wakeShortcut
    @State private var loginItemOn = LaunchAtLogin.isEnabled

    var body: some View {
        Card {
            SectionTitle(text: "Engine", subtitle: "The local process that does the work.")
            InfoRow(label: "Status", value: model.engineStatus.label,
                    color: model.engineStatus.isRunning ? Theme.ready : Theme.attention)
            if let detail = model.engineStatus.detail {
                Text(detail).font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            InfoRow(label: "Engine version", value: model.state?.version ?? model.health?.version ?? "—")
            InfoRow(label: "Listening on", value: "127.0.0.1:\(Prefs.port)", mono: true)
            InfoRow(label: "Engine folder",
                    value: AppPaths.pretty(model.supervisor.engineDirectory?.path ?? Prefs.enginePath ?? "not found"))
            HStack(spacing: 8) {
                Button("Restart Engine") { model.restartEngine() }.auraButton()
                Button("Choose Folder…") { model.chooseEngineFolder() }.auraButton()
                Button("Open Log") { model.openLogFolder() }.auraButton()
            }
        }

        Card {
            SectionTitle(text: "Wake shortcut", subtitle: shortcut.detail)
            Picker("", selection: $shortcut) {
                ForEach(WakeShortcut.allCases, id: \.self) { option in
                    Text(option.label).tag(option)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .onChange(of: shortcut) { value in model.setShortcut(value) }
        }

        Card {
            SectionTitle(text: "At login", subtitle: nil)
            Toggle("Start Aura when I log in", isOn: $loginItemOn)
                .toggleStyle(.switch)
                .onChange(of: loginItemOn) { value in
                    let result = LaunchAtLogin.set(enabled: value)
                    if let message = result {
                        model.toast(message, kind: .warning)
                        loginItemOn = LaunchAtLogin.isEnabled
                    }
                }
            Text("Aura lives in the menu bar — no Dock icon, no windows until you ask.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }

        Card {
            SectionTitle(text: "Your data", subtitle: "Everything stays in Application Support and the log.")
            HStack(spacing: 8) {
                Button("Open data folder") { model.openDataFolder() }.auraButton()
                Button("Open log") { model.openLogFolder() }.auraButton()
            }
            InfoRow(label: "Data folder", value: AppPaths.pretty(AppPaths.dataDirectory.path))
        }
    }
}

// MARK: - Voice

private struct VoiceSection: View {
    @EnvironmentObject var model: AppModel
    @State private var ttsEnabled = true
    @State private var voice = ""
    @State private var rate = 178.0

    private var micGranted: Bool { (model.permissions?.microphone ?? model.micReady) == true }
    private var whisperReady: Bool { model.permissions?.whisperCpp == true }

    var body: some View {
        Card {
            SectionTitle(text: "Microphone",
                         subtitle: "Where Aura listens — audio never leaves this Mac.")
            InfoRow(label: "Microphone",
                    value: micGranted ? "Ready" : "Not granted",
                    color: micGranted ? Theme.ready : Theme.attention)
            HStack(spacing: 8) {
                if !micGranted {
                    Button("Allow microphone") { model.requestPermission("microphone") }
                        .auraButton(prominent: true)
                    Button("Open Microphone settings") { model.openSystemSettings("microphone") }
                        .auraButton()
                } else {
                    Button("Re-check") { Task { await model.refreshPermissions() } }
                        .auraButton()
                }
            }
        }

        Card {
            SectionTitle(text: "Speech", subtitle: "Aura speaks with the system voice — no downloads.")
            Toggle("Let Aura speak replies", isOn: $ttsEnabled)
                .toggleStyle(.switch)
                .onChange(of: ttsEnabled) { value in model.setTTS(value) }

            HStack(spacing: 8) {
                Text("Voice").font(.system(size: 12)).foregroundStyle(.secondary)
                TextField("System default (e.g. Samantha)", text: $voice)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12))
                    .onSubmit { model.setVoice(voice) }
                Button("Use") { model.setVoice(voice) }.auraButton()
            }

            HStack(spacing: 8) {
                Text("Speed").font(.system(size: 12)).foregroundStyle(.secondary)
                Slider(value: $rate, in: 120...260, step: 2, onEditingChanged: { editing in
                    if !editing { model.setSpeechRate(rate) }
                })
                Text("\(Int(rate)) wpm").font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }

        Card {
            SectionTitle(text: "Speech-to-text", subtitle: "On-device transcription (whisper.cpp).")
            InfoRow(label: "Engine", value: model.config?.stt?.engine ?? "—")
            InfoRow(label: "Language", value: model.config?.stt?.language ?? "en")
            InfoRow(label: "whisper.cpp",
                    value: whisperReady ? "Ready" : "Model not downloaded",
                    color: whisperReady ? Theme.ready : Theme.attention)
            if !whisperReady {
                Button("Download speech model") { model.runSetupStep("whisper") }
                    .auraButton(prominent: true)
                    .disabled(model.isInstalling)
            } else {
                Button("Re-check") { Task { await model.refreshPermissions() } }
                    .auraButton()
                    .disabled(model.isInstalling)
            }
        }
        .onAppear(perform: load)
        .onChange(of: model.config?.live["tts"]?.count) { _ in load() }
    }

    private func load() {
        if let live = model.config?.live["tts"] {
            ttsEnabled = live["enabled"]?.boolValue ?? true
            voice = live["voice"]?.stringValue ?? ""
            rate = live["rate"]?.doubleValue ?? 178
        }
    }
}

// MARK: - Understanding (the brain)

private struct BrainSection: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Card {
            SectionTitle(text: "Your local model",
                         subtitle: "Aura thinks with a model that runs on this Mac — Ollama, llama.cpp, LM Studio or MLX.")
            InfoRow(label: "Status",
                    value: model.brainOnline ? "Answering" : "Not answering",
                    color: model.brainOnline ? Theme.ready : Theme.attention)
            InfoRow(label: "Endpoint", value: model.config?.planner?.baseURL ?? model.state?.planner?.baseURL ?? "—", mono: true)
            InfoRow(label: "Model", value: model.config?.planner?.model ?? model.state?.planner?.model ?? "—")
            if let error = model.state?.planner?.lastError, !error.isEmpty {
                Text(error).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Text(model.brainOnline
                 ? "Full understanding is on: Aura can plan multi-step requests in your own words."
                 : "Until a model answers, Aura uses her built-in commands — everything still works, just more literally.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Set up components") { model.installEverything() }
                    .auraButton(prominent: true)
                    .disabled(model.isInstalling)
                    .overlay {
                        if model.isInstalling {
                            ProgressView().controlSize(.small)
                                .offset(x: 60)
                        }
                    }
                Button("Open Ollama") { NSWorkspace.shared.open(URL(string: "https://ollama.com")!) }
                    .auraButton()
            }
        }

        Card {
            SectionTitle(text: "Confidence",
                         subtitle: "How sure Aura must be before acting without asking. Risky actions always ask.")
            HStack(spacing: 10) {
                Slider(value: Binding(get: { model.confidenceThreshold },
                                      set: { model.setConfidence($0) }),
                       in: 0.40...0.95, step: 0.01)
                Text(String(format: "%.2f", model.confidenceThreshold))
                    .font(.system(size: 11, design: .monospaced))
            }
            Text("Lower: Aura acts on vaguer requests. Higher: she asks more often. She never learns her way around the safety gate.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }

        Card {
            SectionTitle(text: "Learned from you", subtitle: "Confirmations and corrections Aura has kept.")
            InfoRow(label: "Examples kept", value: "\(model.metrics?.examples?.total ?? 0)")
        }
    }
}

// MARK: - Safety

private struct SafetySection: View {
    @EnvironmentObject var model: AppModel
    @State private var askBeforeRun = false
    @State private var loaded = false

    var body: some View {
        Card {
            SectionTitle(text: "Before Aura acts", subtitle: nil)
            Toggle("Show me the plan before every action, even safe ones", isOn: $askBeforeRun)
                .toggleStyle(.switch)
                .onChange(of: askBeforeRun) { value in model.setAskBeforeRun(value) }
            Divider().padding(.vertical, 2)
            HStack(spacing: 8) {
                Image(systemName: "lock.fill").font(.system(size: 11)).foregroundStyle(Theme.ready)
                Text("Destructive actions always ask first")
                    .font(.system(size: 12, weight: .medium))
            }
            Text("Emptying the trash, quitting apps with unsaved work, deleting files — Aura stops and shows you exactly what she'll do. There is no setting that turns this off.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }

        Card {
            SectionTitle(text: "Never allowed", subtitle: "Blocked outright, whatever the request.")
            ForEach(["rm -rf /", "diskutil erase", "sudo dd"], id: \.self) { pattern in
                HStack(spacing: 8) {
                    Image(systemName: "nosign").font(.system(size: 11)).foregroundStyle(Theme.danger)
                    Text(pattern).font(.system(size: 11.5, design: .monospaced))
                }
            }
            Text("Add your own lines to blocked_patterns in config.toml (Settings ▸ About shows the path).")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }

        Card {
            SectionTitle(text: "Your control", subtitle: nil)
            HStack(spacing: 8) {
                Button("Open data folder") { model.openDataFolder() }.auraButton()
                Button("Open log") { model.openLogFolder() }.auraButton()
            }
            Text("Aura keeps one SQLite file of what she did and what you taught her. Delete it and she starts over — nothing is hidden anywhere else.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .onAppear {
            guard !loaded else { return }
            askBeforeRun = model.config?.liveValue("safety", "show_plan_before_run")?.boolValue
                ?? model.state?.askBeforeRun ?? false
            loaded = true
        }
    }
}

// MARK: - Permissions / setup

private struct PermissionsSection: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Card {
            SectionTitle(text: "Setup",
                         subtitle: "macOS grants these, not Aura. Each one is revocable in System Settings at any time.")
            if model.isInstalling {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Installing — progress appears below.").font(.system(size: 11))
                }
            }
            Button("Set up everything") { model.installEverything() }
                .auraButton(prominent: true)
                .disabled(model.isInstalling)
            ForEach(model.capabilities) { row in
                CapabilityRowView(row: row)
            }
        }

        if !model.setupProgress.isEmpty {
            Card {
                SectionTitle(text: "Install progress", subtitle: nil)
                ForEach(model.setupProgress.keys.sorted(), id: \.self) { key in
                    InfoRow(label: key.capitalized, value: model.setupProgress[key] ?? "")
                }
            }
        }

        Card {
            SectionTitle(text: "The honest state", subtitle: "Read straight off your Mac, not guessed.")
            InfoRow(label: "Profile", value: model.permissions?.resolvedProfile ?? model.config?.resolvedProfile ?? "—")
            InfoRow(label: "Accessibility",
                    value: model.permissions?.accessibility == true ? "Granted" : "Not granted",
                    color: model.permissions?.accessibility == true ? Theme.ready : Theme.attention)
            InfoRow(label: "Wake models",
                    value: model.permissions?.wakeModels?.ready == true ? "Ready" : "Missing",
                    color: model.permissions?.wakeModels?.ready == true ? Theme.ready : Theme.attention)
            Button("Check again") { Task { await model.refreshPermissions(); await model.refreshAll() } }
                .auraButton()
        }
    }
}

private struct CapabilityRowView: View {
    @EnvironmentObject var model: AppModel
    let row: CapabilityRow

    private var color: Color {
        guard let state = row.state else { return .secondary }
        return state ? Theme.ready : Theme.attention
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: row.state == nil ? "questionmark.circle" : (row.isSatisfied ? "checkmark.circle.fill" : "circle.dashed"))
                .font(.system(size: 13))
                .foregroundStyle(color)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 2) {
                Text(row.title).font(.system(size: 12.5, weight: .medium))
                Text(row.detail)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 8)

            Button(row.actionTitle) { perform(row.action) }
                .auraButton()
                .disabled(model.isInstalling)
        }
        .padding(.vertical, 2)
    }

    private func perform(_ action: CapabilityRow.Action) {
        switch action {
        case .requestMicrophone: model.requestPermission("microphone")
        case .requestAccessibility: model.requestPermission("accessibility")
        case .openAccessibility: model.openSystemSettings("accessibility")
        case .testAutomation: model.testAutomation()
        case .installComponents: model.installEverything()
        case .installWakeModels: model.runSetupStep("wake")
        case .installWhisper: model.runSetupStep("whisper")
        case .openConfig: model.openDataFolder()
        case .none: break
        }
    }
}

// MARK: - Wake phrase

private struct WakeSection: View {
    @EnvironmentObject var model: AppModel
    @State private var phrase = ""
    @State private var mode = "manual"

    var body: some View {
        Card {
            SectionTitle(text: "Listening mode", subtitle: nil)
            Picker("", selection: $mode) {
                Text("When I tap").tag("manual")
                Text("Always listening").tag("openwakeword")
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .onChange(of: mode) { value in model.setWakeMode(value) }
            Text(mode == "manual"
                 ? "Aura listens right after you tap the menu-bar icon or press the shortcut."
                 : "Aura waits quietly for your phrase, on-device, in a few percent of one core.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }

        Card {
            SectionTitle(text: "Your phrase",
                         subtitle: "Teach Aura the phrase in your own voice — a few short recordings, kept on this Mac.")
            HStack(spacing: 8) {
                TextField("e.g. Hey Aura", text: $phrase)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12))
                Button(action: beginTraining) {
                    HStack(spacing: 5) {
                        if model.isStartingTraining { ProgressView().controlSize(.small) }
                        Text(model.isStartingTraining ? "Starting…" : "Set up")
                    }
                }
                .auraButton(prominent: true)
                .disabled(phrase.trimmingCharacters(in: .whitespaces).count < 2
                          || model.isStartingTraining || model.training?.active == true)
            }

            if let training = model.training, training.active {
                Divider().padding(.vertical, 2)
                Text("Say “\(training.phrase ?? phrase)”")
                    .font(.system(size: 13, weight: .semibold))
                HStack(spacing: 6) {
                    ForEach(0..<max(training.need ?? 6, 1), id: \.self) { index in
                        Circle()
                            .fill(index < (training.count ?? 0) ? Theme.ready : Color.primary.opacity(0.15))
                            .frame(width: 12, height: 12)
                    }
                    Text("\(training.count ?? 0) of \(training.need ?? 6)")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Text(training.listening == true
                     ? "Recording… say the phrase now (stops automatically)."
                     : "Record each take naturally. Every tap finishes or reports a problem within five seconds.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    Button {
                        model.captureTrainingSample()
                    } label: {
                        HStack(spacing: 5) {
                            if training.listening == true { ProgressView().controlSize(.small) }
                            Text(training.listening == true ? "Recording…" : "Record a sample")
                        }
                    }
                    .auraButton(prominent: true)
                    .disabled(training.listening == true
                              || (training.count ?? 0) >= (training.need ?? 6)
                              || model.isFinishingTraining)

                    Button {
                        model.finishTraining()
                    } label: {
                        HStack(spacing: 5) {
                            if model.isFinishingTraining { ProgressView().controlSize(.small) }
                            Text(model.isFinishingTraining ? "Training…" : "Train phrase")
                        }
                    }
                    .auraButton()
                    .disabled((training.count ?? 0) < 3 || training.listening == true
                              || model.isFinishingTraining)

                    Button("Start over") { model.cancelTraining() }
                        .auraButton()
                        .disabled(model.isFinishingTraining)
                }
            }

            Text("A second check: while always-listening, commands must begin with this phrase.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .onAppear {
            mode = model.state?.wakeMode ?? "manual"
            phrase = model.state?.wakePhrase ?? ""
        }
        .onChange(of: model.state?.wakeMode) { value in
            if let value { mode = value }
        }
    }

    private func beginTraining() {
        model.beginTraining(phrase)
    }
}

// MARK: - Activity

private struct ActivitySection: View {
    @EnvironmentObject var model: AppModel
    @State private var refreshing = false
    @State private var reloadingLog = false

    var body: some View {
        Card {
            SectionTitle(text: "Recent sessions",
                         subtitle: "Every request, its plan and its outcome — kept on this Mac.")
            Button {
                Task {
                    refreshing = true
                    await model.refreshActivity()
                    refreshing = false
                    model.toast("Activity refreshed.", kind: .success)
                }
            } label: {
                HStack(spacing: 6) {
                    if refreshing {
                        ProgressView().controlSize(.small)
                    }
                    Text(refreshing ? "Refreshing…" : "Refresh")
                }
            }
            .auraButton()
            .disabled(refreshing)

            if model.activity.isEmpty {
                Text("Nothing yet. Ask Aura something and it will land here.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            ForEach(model.activity.prefix(40)) { entry in
                ActivityRow(entry: entry)
                if entry.id != model.activity.prefix(40).last?.id { Divider().opacity(0.4) }
            }
        }

        Card {
            SectionTitle(text: "Engine log", subtitle: "The last lines written by the app and the engine.")
            Button {
                reloadingLog = true
                model.loadLog()
                reloadingLog = false
                model.toast("Log reloaded.", kind: .info)
            } label: {
                HStack(spacing: 6) {
                    if reloadingLog {
                        ProgressView().controlSize(.small)
                    }
                    Text(reloadingLog ? "Reloading…" : "Reload")
                }
            }
            .auraButton()
            .disabled(reloadingLog)
            ScrollView {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(Array(model.logLines.suffix(120).enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .frame(height: 180)
            .padding(6)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.black.opacity(0.12)))
        }
        .onAppear { model.loadLog() }
    }
}

private struct ActivityRow: View {
    @EnvironmentObject var model: AppModel
    let entry: HistoryEntry

    private var color: Color {
        switch entry.outcome {
        case "ok": return Theme.ready
        case "cancelled": return Theme.attention
        default: return Theme.danger
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            StatusDot(color: color, size: 7).padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.transcript)
                    .font(.system(size: 12, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)
                if !entry.reply.isEmpty {
                    Text(entry.reply)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("\(entry.date.formatted(date: .abbreviated, time: .shortened)) · \(entry.totalMs) ms · \(entry.outcome)")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 6)
            HStack(spacing: 4) {
                Button { model.feedback(entry, good: true) } label: {
                    Image(systemName: "hand.thumbsup")
                }
                .auraButton()
                .help("That was right")
                Button { model.feedback(entry, good: false) } label: {
                    Image(systemName: "hand.thumbsdown")
                }
                .auraButton()
                .help("That was wrong — Aura learns from it")
            }
        }
        .padding(.vertical, 4)
    }
}

// MARK: - About

private struct AboutSection: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Card {
            HStack(spacing: 12) {
                OrbView(phase: model.phase, diameter: 54) {}
                VStack(alignment: .leading, spacing: 2) {
                    Text("Aura").font(.system(size: 16, weight: .semibold))
                    Text("Your machine, understood.")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            InfoRow(label: "App version", value: AuraVersion.semantic)
            InfoRow(label: "Engine version", value: model.state?.version ?? "—")
            InfoRow(label: "macOS", value: ProcessInfo.processInfo.operatingSystemVersionString)
            InfoRow(label: "Profile", value: model.permissions?.resolvedProfile ?? model.config?.resolvedProfile ?? "—")
        }

        Card {
            SectionTitle(text: "How Aura works", subtitle: nil)
            Text("• Your voice is transcribed on this Mac.\n• Your words are planned by a model running here.\n• Risky actions stop and ask you first.\n• Nothing is uploaded, ever — there is no account and no telemetry.")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }

        Card {
            SectionTitle(text: "Files", subtitle: nil)
            InfoRow(label: "Settings", value: AppPaths.pretty(AppPaths.userConfigURL.path), mono: true)
            InfoRow(label: "Data", value: AppPaths.pretty(AppPaths.dataDirectory.path), mono: true)
            InfoRow(label: "Log", value: AppPaths.pretty(AppPaths.logURL.path), mono: true)
            HStack(spacing: 8) {
                Button("Open data folder") { model.openDataFolder() }.auraButton()
                Button("Open log") { model.openLogFolder() }.auraButton()
            }
        }
    }
}
