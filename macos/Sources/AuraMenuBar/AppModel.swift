import AppKit
import AuraCore
import Combine
import Foundation
import SwiftUI

// MARK: - small view models

struct PanelMessage: Identifiable, Equatable {
    enum Role: Equatable { case you, aura, system }
    let id = UUID()
    let role: Role
    var text: String
    var time = Date()
    var detail: String?
    var failed = false
}

struct ProposedAction: Identifiable, Equatable {
    let id = UUID()
    let skill: String
    let why: String
    let risk: String
    let verdict: String
    let reasons: [String]

    var isBlocked: Bool { verdict == "blocked" }
    var needsCare: Bool { verdict == "confirm" || risk == "destructive" || risk == "confirm" }

    var title: String {
        let tail = skill.split(separator: ".").last.map(String.init) ?? skill
        return tail.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct Proposal: Identifiable, Equatable {
    let id: String          // the confirmation token
    let reply: String
    let actions: [ProposedAction]
}

struct LiveAction: Identifiable, Equatable {
    enum Status: Equatable { case running, ok, failed }
    let id = UUID()
    let skill: String
    var status: Status
    var message: String?

    var title: String {
        let tail = skill.split(separator: ".").last.map(String.init) ?? skill
        return tail.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct Toast: Identifiable, Equatable {
    enum Kind: Equatable { case info, success, warning, failure }
    let id = UUID()
    let text: String
    var kind: Kind = .info
}

/// What the Setup screen shows for one row of the capability matrix.
struct CapabilityRow: Identifiable {
    enum Action: Equatable {
        case requestMicrophone
        case requestAccessibility
        case openAccessibility
        case testAutomation
        case installComponents
        case installWakeModels
        case installWhisper
        case openConfig
        case none
    }

    let id: String
    let title: String
    let detail: String
    let state: Bool?              // nil = not applicable here
    let action: Action
    let actionTitle: String

    var isSatisfied: Bool { state == true }
}

// MARK: - the app

/// One observable object for the whole UI. It owns the engine connection,
/// folds the SSE stream into view state, and never blocks the main thread.
@MainActor
final class AppModel: ObservableObject {

    // Engine plumbing (not published: the views don't render these directly)
    let client: EngineClient
    let supervisor: EngineSupervisor
    private let log = AuraLog.shared

    // Published state
    @Published private(set) var engineStatus: EngineSupervisor.Status = .stopped
    @Published private(set) var phase: String = "armed"
    @Published private(set) var health: EngineHealth?
    @Published private(set) var state: EngineState?
    @Published private(set) var permissions: PermissionsSnapshot?
    @Published private(set) var config: EngineConfig?
    @Published private(set) var skills: [SkillSpec] = []
    @Published private(set) var activity: [HistoryEntry] = []
    @Published private(set) var metrics: EngineMetrics?
    @Published private(set) var training: WakeTraining?
    @Published private(set) var isStartingTraining = false
    @Published private(set) var isFinishingTraining = false
    @Published private(set) var setupProgress: [String: String] = [:]
    @Published private(set) var messages: [PanelMessage] = []
    @Published private(set) var liveActions: [LiveAction] = []
    @Published private(set) var logLines: [String] = []
    @Published private(set) var proposal: Proposal?
    @Published var toast: Toast?
    @Published var isInstalling = false
    @Published private(set) var lastError: String?
    /// A section the UI should jump to ("settings/setup", "activity", …).
    @Published var requestedSection: String?

    private var eventTask: Task<Void, Never>?
    private var toastTask: Task<Void, Never>?
    private var stallTask: Task<Void, Never>?
    private var thinkingMessageID: UUID?

    init(token: String) {
        let endpoint = EngineEndpoint(host: "127.0.0.1", port: Prefs.port, token: token)
        self.client = EngineClient(endpoint: endpoint)
        self.supervisor = EngineSupervisor(client: client, token: token)

        supervisor.onStatusChange = { [weak self] status in
            Task { @MainActor in self?.engineStatusChanged(status) }
        }
        supervisor.locateEngine = { [weak self] in self?.presentEngineFolderPicker() }
    }

    // MARK: - lifecycle

    func start() {
        log.write("Aura.app: starting (v\(AuraVersion.semantic), port \(Prefs.port))")
        subscribeToEvents()
        supervisor.start()
        Task { await refreshAll() }
    }

    func shutdown() {
        eventTask?.cancel()
        supervisor.stop()
    }

    private func engineStatusChanged(_ status: EngineSupervisor.Status) {
        engineStatus = status
        if case .failed(let message) = status {
            lastError = message
            toast(message, kind: .failure)
            appendSystem(message)
        }
        if status.isRunning {
            Task { await refreshAll() }
        }
    }

    // MARK: - the event stream

    private func subscribeToEvents() {
        eventTask?.cancel()
        eventTask = Task { [weak self] in
            guard let self else { return }
            for await event in self.client.events() {
                if Task.isCancelled { return }
                self.handle(event)
            }
        }
    }

    /// UI-side backstop for the engine's five-second active-work deadline.
    /// One timer spans planning → executing; phase changes must not restart it.
    /// If SSE drops at exactly the wrong moment, the user still never stares at
    /// a permanent “Thinking…”.
    private func watchForStall() {
        let activeWork = ["planning", "executing", "responding"].contains(phase)
        guard activeWork else {
            stallTask?.cancel()
            stallTask = nil
            return
        }
        guard stallTask == nil else { return }
        stallTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 5_000_000_000) }
            catch { return }
            guard !Task.isCancelled, let self, self.thinkingMessageID != nil else { return }

            // Deliver the deadline locally first; a state refresh must not add
            // network latency to the product's visible response guarantee.
            self.stallTask = nil
            self.appendAura("That took too long (over five seconds), so I stopped waiting. Please try again.",
                            failed: true)
            let snapshot = try? await self.client.state(timeout: 1)
            if let snapshot {
                self.state = snapshot
                self.phase = snapshot.state
                if snapshot.state == "armed" { self.proposal = nil }
            }
        }
    }

    private func handle(_ event: EngineEvent) {
        switch event.type {
        case "state":
            if let value = event.state {
                if value != phase { phase = value }
                if value == "planning" { liveActions.removeAll() }
                if value == "armed" {
                    proposal = nil
                    Task { await refreshAfterSession() }
                }
                watchForStall()
            }

        case "transcript":
            if let text = event.text, !text.isEmpty { appendUser(text) }

        case "proposal":
            proposal = Proposal(id: event.token ?? "",
                                reply: event.data["reply"]?.stringValue ?? "",
                                actions: (event.data["actions"]?.arrayValue ?? []).map { value in
                                    ProposedAction(skill: value["skill"]?.stringValue ?? "action",
                                                   why: value["why"]?.stringValue ?? "",
                                                   risk: value["risk"]?.stringValue ?? "safe",
                                                   verdict: value["verdict"]?.stringValue ?? "confirm",
                                                   reasons: (value["reasons"]?.arrayValue ?? [])
                                                       .compactMap { $0.stringValue })
                                })
            if let reply = event.data["reply"]?.stringValue, !reply.isEmpty {
                appendSystem("Plan ready — \(reply)")
            }

        case "action_started":
            let skill = event.skill ?? "action"
            if !liveActions.contains(where: { $0.skill == skill && $0.status == .running }) {
                liveActions.append(LiveAction(skill: skill, status: .running))
            }

        case "action_result":
            let skill = event.skill ?? "action"
            let ok = event.data["ok"]?.boolValue ?? true
            let message = event.message
            if let index = liveActions.lastIndex(where: { $0.skill == skill && $0.status == .running }) {
                liveActions[index].status = ok ? .ok : .failed
                liveActions[index].message = message
            } else {
                liveActions.append(LiveAction(skill: skill, status: ok ? .ok : .failed, message: message))
            }

        case "reply":
            let text = event.text ?? ""
            if !text.isEmpty {
                let failed = (event.data["outcome"]?.stringValue ?? "ok") == "failed"
                let isDeadline = text.localizedCaseInsensitiveContains("five seconds")
                let alreadyShowedDeadline = messages.last?.failed == true
                    && messages.last?.text.localizedCaseInsensitiveContains("five seconds") == true
                if !isDeadline || !alreadyShowedDeadline {
                    appendAura(text, failed: failed)
                }
            }

        case "hint":
            if let text = event.text, !text.isEmpty { appendSystem(text) }

        case "log":
            if let line = event.line, !line.isEmpty {
                logLines.append(line)
                if logLines.count > 400 { logLines.removeFirst(logLines.count - 400) }
            }

        case "train_update":
            let trainingPhase = event.data["phase"]?.stringValue ?? "idle"
            let prior = training
            training = WakeTraining(active: trainingPhase != "idle" && trainingPhase != "done",
                                    phrase: event.data["phrase"]?.stringValue ?? prior?.phrase,
                                    count: event.data["count"]?.intValue ?? prior?.count,
                                    need: event.data["need"]?.intValue ?? prior?.need,
                                    listening: trainingPhase == "listening")
            if trainingPhase == "done" {
                let threshold = event.data["threshold"]?.doubleValue
                toast("Wake phrase trained" + (threshold.map { String(format: " (confidence %.2f)", $0) } ?? ""),
                      kind: .success)
            }

        case "train_sample":
            if let message = event.message {
                toast(message, kind: (event.data["ok"]?.boolValue ?? true) ? .success : .warning)
            }
            training = WakeTraining(active: true,
                                    phrase: training?.phrase,
                                    count: event.data["count"]?.intValue ?? training?.count,
                                    need: event.data["need"]?.intValue ?? training?.need,
                                    listening: false)

        case "setup_progress":
            let key = event.data["key"]?.stringValue ?? "step"
            let status = event.data["status"]?.stringValue ?? ""
            let detail = event.data["detail"]?.stringValue ?? ""
            setupProgress[key] = detail.isEmpty ? status : "\(status) — \(detail)"
            if status == "running" { isInstalling = true }
            if ["ok", "fail", "skip"].contains(status) {
                if setupProgress.values.allSatisfy({ !$0.hasPrefix("running") }) { isInstalling = false }
                toast("\(event.data["title"]?.stringValue ?? "Step"): \(status)", kind: status == "ok" ? .success : .warning)
            }

        case "setup_done":
            isInstalling = false
            setupProgress.removeAll()
            let ok = event.data["ok"]?.boolValue ?? false
            let summary = event.data["summary"]?.stringValue ?? "Setup complete."
            toast(summary, kind: ok ? .success : .warning)
            Task { await refreshAll() }

        case "config":
            Task { await refreshConfig() }

        case "wake_fallback":
            if let reason = event.reason { toast(reason, kind: .warning) }

        default:
            break
        }
    }

    // MARK: - messages

    private func appendUser(_ text: String) {
        messages.append(PanelMessage(role: .you, text: text))
        trimMessages()
        beginThinking()
    }

    private func appendAura(_ text: String, failed: Bool) {
        stallTask?.cancel()
        stallTask = nil
        thinkingMessageID = nil
        messages.removeAll { $0.text == "Thinking…" && $0.role == .aura }
        messages.append(PanelMessage(role: .aura, text: text, failed: failed))
        trimMessages()
    }

    private func appendSystem(_ text: String) {
        if messages.last?.text == text { return }
        messages.append(PanelMessage(role: .system, text: text))
        trimMessages()
    }

    private func beginThinking() {
        guard thinkingMessageID == nil else { return }
        let message = PanelMessage(role: .aura, text: "Thinking…")
        thinkingMessageID = message.id
        messages.append(message)
    }

    private func trimMessages() {
        if messages.count > 60 { messages.removeFirst(messages.count - 60) }
    }

    /// Show a transient message. Auto-dismisses so nothing has to be closed.
    func toast(_ text: String, kind: Toast.Kind = .info) {
        toastTask?.cancel()
        withAnimation(.easeOut(duration: 0.15)) { toast = Toast(text: text, kind: kind) }
        toastTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 4_500_000_000)
            guard !Task.isCancelled else { return }
            withAnimation(.easeIn(duration: 0.2)) { self?.toast = nil }
        }
    }

    func clearConversation() {
        messages.removeAll()
        liveActions.removeAll()
        proposal = nil
    }

    // MARK: - actions

    func wake() {
        Task {
            do {
                _ = try await client.trigger()
            } catch let error as EngineError where error.isUnauthorized {
                handleUnauthorized()
            } catch {
                toast(error.localizedDescription, kind: .warning)
            }
        }
    }

    func send(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        appendUser(trimmed)
        Task {
            do {
                let reply = try await client.send(text: trimmed)
                if reply.accepted == false {
                    let message = reply.message ?? "Aura is busy."
                    appendSystem(message)
                    toast(message, kind: .warning)
                    messages.removeAll { $0.text == "Thinking…" }
                }
            } catch let error as EngineError where error.isUnauthorized {
                handleUnauthorized()
            } catch {
                appendAura(error.localizedDescription, failed: true)
            }
        }
    }

    func confirmProposal() {
        guard let proposal else { return }
        self.proposal = nil
        Task {
            do {
                let reply = try await client.confirm(token: proposal.id)
                if reply.ok == false { toast("That request expired — ask again.", kind: .warning) }
            } catch {
                toast(error.localizedDescription, kind: .failure)
            }
        }
    }

    func cancelProposal() {
        guard let proposal else { return }
        self.proposal = nil
        Task { _ = try? await client.cancel(token: proposal.id) }
    }

    private func handleUnauthorized() {
        let message = "Aura's engine only answers the app that started it. "
            + "Use “Restart Engine” in the menu."
        lastError = message
        toast(message, kind: .failure)
    }

    // MARK: - refreshing

    func refreshAll() async {
        if !engineStatus.isRunning {
            let alive = (try? await client.health(timeout: 1.5)) != nil
            guard alive else { return }
        }
        async let healthTask = client.health()
        async let stateTask = client.state()
        async let permissionsTask = client.permissions()
        async let configTask = client.config()
        async let skillsTask = client.skills()
        async let historyTask = client.history()
        async let metricsTask = client.metrics()
        async let trainingTask = client.training()

        health = try? await healthTask

        let stateSnapshot = try? await stateTask
        if let stateSnapshot {
            state = stateSnapshot
            phase = stateSnapshot.state
        }

        permissions = try? await permissionsTask
        config = try? await configTask

        let skillList = try? await skillsTask
        if let skillList { skills = skillList }

        let historyList = try? await historyTask
        if let historyList { activity = historyList }

        metrics = try? await metricsTask

        let trainingSnapshot = try? await trainingTask
        if let trainingSnapshot { training = trainingSnapshot.active ? trainingSnapshot : nil }
    }

    private func refreshAfterSession() async {
        let snapshot = try? await client.state()
        if let snapshot { state = snapshot; phase = snapshot.state }
        let list = try? await client.history()
        if let list { activity = list }
    }

    func refreshConfig() async {
        config = try? await client.config()
        let snapshot = try? await client.state()
        if let snapshot { state = snapshot; phase = snapshot.state }
    }

    func refreshActivity() async {
        let list = try? await client.history()
        if let list { activity = list }
    }

    func refreshPermissions() async {
        permissions = try? await client.permissions()
        let snapshot = try? await client.state()
        if let snapshot { state = snapshot }
    }

    private func refreshTraining() async {
        let snapshot = try? await client.training()
        if let snapshot { training = snapshot.active ? snapshot : nil }
    }

    // MARK: - settings

    func setTTS(_ enabled: Bool) {
        applyConfig(section: "tts", key: "enabled", value: .bool(enabled))
    }

    func setAskBeforeRun(_ enabled: Bool) {
        applyConfig(section: "safety", key: "show_plan_before_run", value: .bool(enabled))
    }

    func setWakeMode(_ mode: String) {
        Task {
            let wakeResult = try? await client.setWakeMode(mode)
            if let reply = wakeResult, reply.ok == false {
                toast(reply.message ?? "That didn't take.", kind: .warning)
            }
            await refreshConfig()
            await refreshPermissions()
        }
    }

    func setWakePhrase(_ phrase: String) {
        Task {
            _ = try? await client.setWakeMode(state?.wakeMode ?? "manual", phrase: phrase)
            await refreshConfig()
        }
    }

    func setConfidence(_ value: Double) {
        applyConfig(section: "laya", key: "confidence_threshold", value: .number(value))
    }

    func setVoice(_ voice: String) {
        applyConfig(section: "tts", key: "voice", value: .string(voice))
    }

    func setSpeechRate(_ rate: Double) {
        applyConfig(section: "tts", key: "rate", value: .number(rate))
    }

    func setShortcut(_ shortcut: WakeShortcut) {
        Prefs.wakeShortcut = shortcut
        NotificationCenter.default.post(name: .auraShortcutChanged, object: nil)
    }

    private func applyConfig(section: String, key: String, value: JSONValue) {
        Task {
            do {
                let reply = try await client.updateConfig(section: section, key: key, value: value)
                if reply.ok == false {
                    toast(reply.message ?? "Aura couldn't save that.", kind: .warning)
                }
            } catch {
                toast(error.localizedDescription, kind: .warning)
            }
            await refreshConfig()
        }
    }

    // MARK: - permissions & setup

    func requestPermission(_ target: String) {
        toast("Asking macOS…", kind: .info)
        Task {
            if target == "microphone" {
                let granted = await Permissions.requestMicrophone()
                if !granted {
                    toast("Allow Aura under System Settings › Privacy & Security › Microphone.",
                          kind: .warning)
                    await refreshPermissions()
                    return
                }
            }
            do {
                // The engine may have started with SilentMic before the native
                // TCC answer. This call both verifies the device and hot-attaches
                // the real stream; no engine restart is required.
                let answer = try await client.requestPermission(target)
                toast(answer.message ?? "Checked.", kind: answer.ok ? .success : .warning)
            } catch {
                toast(error.localizedDescription, kind: .failure)
            }
            await refreshPermissions()
        }
    }

    func openSystemSettings(_ target: String) {
        Task {
            let answer = try? await client.openSystemSettings(target)
            if let answer = answer, answer.ok == false {
                toast(answer.message ?? "Couldn't open settings.", kind: .warning)
            }
        }
    }

    func testAutomation() {
        Task {
            let answer = try? await client.testAutomation()
            if let answer {
                toast(answer.message ?? "Tested.", kind: answer.ok ? .success : .warning)
            }
            await refreshPermissions()
        }
    }

    func runSetupStep(_ step: String) {
        guard !isInstalling else {
            toast("Another install step is running — please wait.", kind: .warning)
            return
        }
        isInstalling = true
        Task {
            defer { isInstalling = false }
            do {
                let answer = try await client.runSetupStep(step)
                toast(answer.detail ?? answer.message ?? "Done.", kind: answer.ok ? .success : .warning)
            } catch {
                toast("Couldn't reach Aura's engine.", kind: .failure)
            }
            await refreshPermissions()
        }
    }

    func installEverything() {
        guard !isInstalling else {
            toast("Install already in progress — check Setup for details.", kind: .warning)
            return
        }
        isInstalling = true
        Task {
            defer { isInstalling = false }
            do {
                let reply = try await client.startInstall()
                if reply.ok == false {
                    toast(reply.message ?? "Install didn't start.", kind: .warning)
                } else {
                    toast("Install started — follow progress in Setup.", kind: .info)
                    // Jump to Permissions/Setup so the user can see progress.
                    requestedSection = "settings/setup"
                }
            } catch {
                toast("Couldn't reach Aura's engine. Is it running?", kind: .failure)
            }
        }
    }

    // MARK: - wake phrase studio

    func beginTraining(_ phrase: String) {
        guard !isStartingTraining && !isFinishingTraining else { return }
        isStartingTraining = true
        Task {
            defer { isStartingTraining = false }
            do {
                let start = try await client.startTraining(phrase: phrase)
                if start.ok == false {
                    toast(start.message ?? "Couldn't start training.", kind: .warning)
                    return
                }
                training = WakeTraining(active: true, phrase: phrase.lowercased(),
                                        count: 0, need: start.need ?? 6, listening: false)
                toast("Ready — tap Record, then say your phrase.", kind: .info)
            } catch {
                toast(error.localizedDescription, kind: .failure)
            }
        }
    }

    func captureTrainingSample() {
        guard let current = training, current.active, current.listening != true,
              !isFinishingTraining else { return }

        // Flip the UI synchronously, before even the loopback request. The tap
        // therefore always has visible feedback and duplicate taps are blocked.
        training = WakeTraining(active: true, phrase: current.phrase,
                                count: current.count, need: current.need,
                                listening: true)
        toast("Listening… say it now.", kind: .info)
        Task {
            do {
                let capture = try await client.captureSample()
                if capture.ok == false {
                    toast(capture.message ?? "Couldn't record that.", kind: .warning)
                    await refreshTraining()
                }
            } catch {
                toast(error.localizedDescription, kind: .failure)
                await refreshTraining()
            }
        }
    }

    func finishTraining() {
        guard !isFinishingTraining, training?.listening != true else { return }
        isFinishingTraining = true
        toast("Training your phrase…", kind: .info)
        Task {
            defer { isFinishingTraining = false }
            do {
                let finish = try await client.finishTraining()
                if finish.ok == false {
                    // Keep the accepted samples on screen so the user can fix
                    // the problem or retry; the old UI discarded everything.
                    toast(finish.message ?? "Training didn't take.", kind: .warning)
                    await refreshTraining()
                    return
                }
                training = nil
                toast("Trained — Aura now listens for your phrase.", kind: .success)
                await refreshPermissions()
                await refreshConfig()
            } catch {
                toast(error.localizedDescription, kind: .failure)
                await refreshTraining()
            }
        }
    }

    func cancelTraining() {
        guard !isFinishingTraining else { return }
        training = nil
        Task {
            do {
                let result = try await client.cancelTraining()
                if result.ok == false {
                    toast(result.message ?? "Couldn't start over.", kind: .warning)
                    await refreshTraining()
                }
            } catch {
                toast(error.localizedDescription, kind: .warning)
                await refreshTraining()
            }
            await refreshPermissions()
        }
    }

    // MARK: - feedback + files

    func feedback(_ entry: HistoryEntry, good: Bool) {
        Task {
            _ = try? await client.feedback(transcript: entry.transcript,
                                           skill: "",
                                           good: good)
            toast(good ? "Thanks — noted." : "Got it — Aura will be more careful.",
                  kind: .success)
            await refreshActivity()
        }
    }

    func openDataFolder() {
        Task {
            let opened = try? await client.openFolder("data")
            if let reply = opened, reply.ok == false {
                NSWorkspace.shared.open(AppPaths.dataDirectory)
            }
        }
    }

    func openLogFolder() {
        Task { _ = try? await client.openFolder("logs") }
    }

    func loadLog() {
        logLines = log.tail(lines: 400)
    }

    func restartEngine() {
        toast("Restarting Aura's engine…", kind: .info)
        lastError = nil
        supervisor.restart()
    }

    func chooseEngineFolder() {
        supervisor.chooseEngineFolder()
    }

    private func presentEngineFolderPicker() -> String? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose the mac-assistant folder (the one containing the aura package)"
        panel.prompt = "Use this folder"
        return panel.runModal() == .OK ? panel.url?.path : nil
    }

    // MARK: - derived values the views use

    var isBusy: Bool {
        ["capturing", "transcribing", "planning", "executing", "responding"].contains(phase)
    }

    var isListening: Bool { phase == "capturing" }

    var awaitingConfirmation: Bool { proposal != nil || phase == "proposing" }

    var phaseLabel: String {
        switch phase {
        case "armed": return "Ready when you are"
        case "capturing": return "Listening…"
        case "transcribing": return "Writing down what you said…"
        case "planning": return "Thinking…"
        case "proposing": return "Waiting for your go-ahead"
        case "executing": return "Doing it…"
        case "responding": return "Answering"
        case "disabled": return "Off"
        default: return phase.capitalized
        }
    }

    var phaseDetail: String {
        switch engineStatus {
        case .failed(let message): return message
        case .starting: return "Starting Aura's engine…"
        case .stopped: return "Aura's engine is stopped"
        case .running(let external):
            return external ? "Engine already running — attached" : engineSummary
        }
    }

    var engineSummary: String {
        guard let metrics else { return "Engine ready" }
        let engine = metrics.sttEngine ?? "speech"
        let memory = metrics.rssMB.map { String(format: "%.0f MB", $0) } ?? "—"
        return "\(engine) · \(memory) resident"
    }

    var micReady: Bool { state?.micReady ?? permissions?.microphone ?? false }

    var brainOnline: Bool { state?.plannerOnline ?? health?.plannerOnline ?? false }

    var isDemoProfile: Bool {
        (permissions?.resolvedProfile ?? config?.resolvedProfile ?? state?.profile) == "demo"
    }

    var confidenceThreshold: Double {
        guard let value = config?.liveValue("laya", "confidence_threshold")?.doubleValue else { return 0.62 }
        return value
    }

    var capabilities: [CapabilityRow] {
        let microphone = permissions?.microphone ?? state?.micReady
        let accessibility = permissions?.accessibility
        let wakeReady = permissions?.wakeModels?.ready ?? false
        let wakeDetail = permissions?.wakeModels?.detail ?? "Wake words come from the pretrained model set."
        let whisper = permissions?.whisperCpp
        let planner = permissions?.plannerServer
        let isMac = permissions?.isMac ?? false

        return [
            CapabilityRow(
                id: "microphone", title: "Microphone",
                detail: microphone == true
                    ? "Aura can hear you — audio never leaves this Mac."
                    : "Aura needs the microphone to hear your wake phrase and commands.",
                state: microphone,
                action: .requestMicrophone,
                actionTitle: microphone == true ? "Check again" : "Allow microphone"),
            CapabilityRow(
                id: "accessibility", title: "Accessibility",
                detail: accessibility == true
                    ? "Aura can read and click inside your apps, as you ask."
                    : "Lets Aura see and click inside other apps. macOS asks you; Aura never guesses.",
                state: accessibility,
                action: accessibility == true ? .openAccessibility : .requestAccessibility,
                actionTitle: accessibility == true ? "Open System Settings" : "Grant access"),
            CapabilityRow(
                id: "automation", title: "Automation",
                detail: isMac
                    ? "macOS asks per app the first time Aura drives it. This checks the dialogs work."
                    : "AppleScript automation is a macOS feature.",
                state: nil,
                action: .testAutomation,
                actionTitle: "Run a test"),
            CapabilityRow(
                id: "whisper", title: "Speech-to-text",
                detail: whisper == true
                    ? "whisper.cpp is installed and pointed at a model."
                    : "Aura transcribes on-device with whisper.cpp. Downloading the model is one click.",
                state: whisper,
                action: .installWhisper,
                actionTitle: "Download model"),
            CapabilityRow(
                id: "planner", title: "Aura's mind",
                detail: planner == true
                    ? "The Laya decision model is loaded — full understanding is on."
                    : "Aura runs on built-in commands until the Laya checkpoint loads.",
                state: planner,
                action: .none,
                actionTitle: planner == true ? "Online" : "See how to connect"),
            CapabilityRow(
                id: "wake", title: "Wake words",
                detail: wakeDetail,
                state: wakeReady,
                action: .installWakeModels,
                actionTitle: wakeReady ? "Installed" : "Download models"),
        ]
    }
}

extension Notification.Name {
    /// Posted when the global shortcut changes, so the hotkey can re-register.
    static let auraShortcutChanged = Notification.Name("AuraShortcutChanged")
    /// Posted when the panel/window should be shown (menu, shortcut, dock).
    static let auraOpenPanel = Notification.Name("AuraOpenPanel")
}
