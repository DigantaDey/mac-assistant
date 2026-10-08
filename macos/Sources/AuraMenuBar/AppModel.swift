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
    /// When the question arrived and how long the engine will wait for an
    /// answer — the card counts down, because a confirmation that silently
    /// expires is how "Empty the trash" once died with nobody watching.
    let receivedAt: Date
    let timeout: TimeInterval
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

struct ActivityFeedbackState: Equatable {
    enum Vote: Equatable { case good, bad }
    enum Phase: Equatable { case sending, sent, failed }

    let vote: Vote
    let phase: Phase
}

/// What the Setup screen shows for one row of the capability matrix.
struct CapabilityRow: Identifiable {
    enum Action: Equatable {
        case requestMicrophone
        case requestAccessibility
        case openAccessibility
        case testAutomation
        case requestNotifications
        case openNotificationSettings
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
    /// Read in Aura.app itself: a child Python probe can disagree with the
    /// TCC identity that owns the native Accessibility grant.
    @Published private(set) var nativeAccessibilityGranted: Bool?
    /// The engine's own live reading. The engine is the process that actually
    /// drives your apps, so when it can answer, it is the authority on whether
    /// Aura can act — and it names the app macOS filed the grant under.
    @Published private(set) var engineAccessibilityGranted: Bool?
    @Published private(set) var engineAccessibilityDetail: String?
    @Published private(set) var automationTestMessage: String?
    @Published private(set) var automationTestSucceeded: Bool?
    @Published private(set) var config: EngineConfig?
    @Published private(set) var skills: [SkillSpec] = []
    @Published private(set) var activity: [HistoryEntry] = []
    @Published private(set) var metrics: EngineMetrics?
    @Published private(set) var training: WakeTraining?
    @Published private(set) var isStartingTraining = false
    @Published private(set) var isFinishingTraining = false
    @Published private(set) var isTestingAutomation = false
    @Published private(set) var isChangingWakeMode = false
    @Published private(set) var feedbackByEntryID: [Int: ActivityFeedbackState] = [:]
    @Published private(set) var setupProgress: [String: String] = [:]
    @Published private(set) var messages: [PanelMessage] = []
    @Published private(set) var liveActions: [LiveAction] = []
    @Published private(set) var logLines: [String] = []
    @Published private(set) var proposal: Proposal?
    /// Whether macOS will actually show Aura's notifications — read live, and
    /// surfaced in Setup, because a denied grant turns every proposal alert
    /// into silence (and an ad-hoc rebuild can orphan the grant unnoticed).
    @Published private(set) var notificationsAllowed: Bool?
    /// Proposals already delivered as a notification, so re-delivery (panel
    /// closing again, re-opening, …) never double-banners the same question.
    private var notifiedProposalTokens: Set<String> = []
    @Published var toast: Toast?
    @Published var isInstalling = false
    @Published private(set) var lastError: String?
    /// A section the UI should jump to ("settings/setup", "activity", …).
    @Published var requestedSection: String?

    private var eventTask: Task<Void, Never>?
    private var toastTask: Task<Void, Never>?
    private var stallTask: Task<Void, Never>?
    private var thinkingMessageID: UUID?
    private var activeSessionID: String?
    private var activePlanReply: String?
    private var automationProbeID: UUID?
    private var automationTimeoutTask: Task<Void, Never>?
    private var accessibilityWatchTask: Task<Void, Never>?
    private var accessibilityObserver: NSObjectProtocol?

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
        nativeAccessibilityGranted = Permissions.accessibilityGranted
        subscribeToEvents()
        watchAccessibility()
        supervisor.start()
        Task { await refreshAll() }
        Task { await refreshNotificationStatus() }
    }

    func shutdown() {
        eventTask?.cancel()
        accessibilityWatchTask?.cancel()
        accessibilityWatchTask = nil
        if let accessibilityObserver {
            DistributedNotificationCenter.default().removeObserver(accessibilityObserver)
        }
        accessibilityObserver = nil
        supervisor.stop()
    }

    // MARK: - noticing an Accessibility grant on its own

    /// Keep watching until the user's switch is reflected here — no "Check
    /// again" button, no restart, no stale "not granted" beside a grant the
    /// user can see in System Settings.
    ///
    /// Two triggers, because neither alone is reliable: macOS posts
    /// `com.apple.accessibility.api` when the setting changes (but can post it
    /// before tccd commits), and a slow poll catches the commit itself. The
    /// poll backs off once Aura is trusted — there is nothing left to wait for,
    /// but a revocation should still be noticed.
    private func watchAccessibility() {
        accessibilityWatchTask?.cancel()
        if accessibilityObserver == nil {
            accessibilityObserver = DistributedNotificationCenter.default()
                .addObserver(forName: Notification.Name("com.apple.accessibility.api"),
                             object: nil, queue: .main) { [weak self] _ in
                    // Bind to a constant first: a weak `self` is a captured
                    // *var*, which concurrently-executing code may not touch.
                    guard let model = self else { return }
                    Task { @MainActor in
                        // Give tccd a moment to commit before reading.
                        try? await Task.sleep(nanoseconds: 400_000_000)
                        await model.recheckAccessibility()
                    }
                }
        }
        accessibilityWatchTask = Task { [weak self] in
            guard let self else { return }
            // This Task inherits the main actor from watchAccessibility(), so
            // the published state below is read where it is written.
            while !Task.isCancelled {
                let granted = self.nativeAccessibilityGranted ?? false
                let nap: UInt64 = granted ? 5_000_000_000 : 1_000_000_000
                do { try await Task.sleep(nanoseconds: nap) } catch { return }
                guard !Task.isCancelled else { return }
                await self.recheckAccessibility()
            }
        }
    }

    /// Re-read the native grant and, when it changed, say so and re-sync.
    private func recheckAccessibility() async {
        let live = Permissions.accessibilityGranted
        guard live != nativeAccessibilityGranted else { return }
        let wasGranted = nativeAccessibilityGranted
        nativeAccessibilityGranted = live
        log.write("Aura.app: accessibility \(live ? "granted" : "no longer granted")")
        if live {
            toast("Accessibility granted — Aura can act inside your apps.", kind: .success)
            appendSystem("Accessibility granted.")
        } else if wasGranted == true {
            toast("macOS no longer reports Accessibility for Aura.", kind: .warning)
        }
        await refreshPermissions()
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

    /// UI-side backstop for the engine's 20-second active-work deadline.
    /// One timer spans planning → executing; phase changes must not restart it.
    /// If SSE drops, query the engine first so the UI never says a finished
    /// action is still thinking (or says it stopped while the engine can act).
    private func watchForStall() {
        let activeWork = ["planning", "executing", "responding"].contains(phase)
        guard activeWork else {
            stallTask?.cancel()
            stallTask = nil
            return
        }
        guard stallTask == nil else { return }
        stallTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 22_000_000_000) }
            catch { return }
            guard !Task.isCancelled, let self, self.thinkingMessageID != nil else { return }

            self.stallTask = nil
            let snapshot = try? await self.client.state(timeout: 1)
            guard !Task.isCancelled, self.thinkingMessageID != nil else { return }
            if let snapshot {
                self.state = snapshot
                self.phase = snapshot.state
                if snapshot.state == "armed" {
                    self.proposal = nil
                    self.notifiedProposalTokens.removeAll()
                    self.recoverMissingReply()
                    return
                }
            }
            self.appendAura("Aura hasn't returned a result yet. Check Activity or restart the engine.",
                            failed: true)
        }
    }

    private func handle(_ event: EngineEvent) {
        switch event.type {
        case "state":
            if let value = event.state {
                let previousPhase = phase
                if value != phase { phase = value }
                if value == "planning" {
                    liveActions.removeAll()
                    activePlanReply = nil
                    activeSessionID = event.session
                } else if ["proposing", "executing", "responding"].contains(value),
                          let session = event.session {
                    activeSessionID = session
                }
                if value == "executing", proposal == nil, thinkingMessageID == nil {
                    beginThinking()
                }
                if value == "armed" {
                    let sessionMatches = event.session != nil
                        && event.session == activeSessionID
                    let legacyTerminalTransition = event.session == nil
                        && previousPhase != "armed"
                    if sessionMatches || legacyTerminalTransition {
                        recoverMissingReply()
                    }
                    proposal = nil
                    notifiedProposalTokens.removeAll()
                    Notify.clearProposals()
                    Task { await refreshAfterSession() }
                }
                watchForStall()
            }

        case "plan":
            if let session = event.session { activeSessionID = session }
            activePlanReply = event.data["reply"]?.stringValue

        case "transcript":
            if let text = event.text, !text.isEmpty { appendUser(text) }

        case "proposal":
            if let session = event.session { activeSessionID = session }
            removeThinkingIndicator()
            let token = event.token ?? ""
            let proposalActions = (event.data["actions"]?.arrayValue ?? []).map { value in
                ProposedAction(skill: value["skill"]?.stringValue ?? "action",
                               why: value["why"]?.stringValue ?? "",
                               risk: value["risk"]?.stringValue ?? "safe",
                               verdict: value["verdict"]?.stringValue ?? "confirm",
                               reasons: (value["reasons"]?.arrayValue ?? [])
                                   .compactMap { $0.stringValue })
            }
            let pending = Proposal(id: token,
                                   reply: event.data["reply"]?.stringValue ?? "",
                                   actions: proposalActions,
                                   receivedAt: Date(),
                                   timeout: confirmationTimeout)
            proposal = pending
            if let reply = event.data["reply"]?.stringValue, !reply.isEmpty {
                appendSystem("Plan ready — \(reply)")
            }
            deliverProposalQuestion(pending)

        case "action_started":
            if let session = event.session { activeSessionID = session }
            let skill = event.skill ?? "action"
            if !liveActions.contains(where: { $0.skill == skill && $0.status == .running }) {
                liveActions.append(LiveAction(skill: skill, status: .running))
            }

        case "action_result":
            if let session = event.session { activeSessionID = session }
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
            if let replySession = event.session, activeSessionID != replySession { return }
            if activeSessionID == nil && thinkingMessageID == nil { return }
            // A reply is terminal user-facing state. The engine publishes its
            // armed transition immediately after this event; treating the
            // result as complete here also prevents a missed state frame from
            // leaving the composer permanently busy.
            phase = "armed"
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

        case "history_updated":
            Task { _ = await refreshActivity() }

        case "train_update":
            let trainingPhase = event.data["phase"]?.stringValue ?? "idle"
            let prior = training
            training = WakeTraining(active: trainingPhase != "idle" && trainingPhase != "done",
                                    phrase: event.data["phrase"]?.stringValue ?? prior?.phrase,
                                    count: event.data["count"]?.intValue ?? prior?.count,
                                    need: event.data["need"]?.intValue ?? prior?.need,
                                    listening: trainingPhase == "listening",
                                    message: event.data["message"]?.stringValue ?? prior?.message)
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
                                    listening: false,
                                    message: event.message ?? training?.message)

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
            Task {
                let snapshot = try? await client.state()
                if let snapshot { state = snapshot }
            }

        case "accessibility":
            // The engine polls its own live grant and speaks up when it
            // changes. This is the answer that matters for whether Aura can
            // actually act, and it arrives without the user asking.
            let granted = event.data["granted"]?.boolValue
            engineAccessibilityDetail = event.data["detail"]?.stringValue
            let alreadyKnown = engineAccessibilityGranted == granted
            engineAccessibilityGranted = granted
            guard !alreadyKnown else { return }
            if let detail = event.data["detail"]?.stringValue, !detail.isEmpty {
                appendSystem(detail)
            }
            if granted == true, nativeAccessibilityGranted != true {
                // The engine is trusted and Aura.app is not — worth knowing,
                // because it means the grant belongs to another copy.
                nativeAccessibilityGranted = Permissions.accessibilityGranted
            }
            Task { await refreshPermissions() }

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
        activeSessionID = nil
        activePlanReply = nil
        messages.removeAll { $0.text == "Thinking…" && $0.role == .aura }
        messages.append(PanelMessage(role: .aura, text: text, failed: failed))
        trimMessages()
    }

    private func removeThinkingIndicator() {
        guard let thinkingMessageID else { return }
        messages.removeAll { $0.id == thinkingMessageID }
        self.thinkingMessageID = nil
        stallTask?.cancel()
        stallTask = nil
    }

    /// Final `armed` state is authoritative. If a reply event was lost while
    /// SSE was reconnecting, replace the stale spinner with the most grounded
    /// result we saw; never leave the conversation in a permanent thinking state.
    private func recoverMissingReply() {
        guard thinkingMessageID != nil else { return }
        let failures = liveActions.filter { $0.status == .failed }
        if !failures.isEmpty {
            let details = failures.compactMap(\.message)
            appendAura(details.isEmpty ? "Aura couldn't complete that action. Check Activity for details."
                       : details.joined(separator: " "), failed: true)
            return
        }
        let completed = liveActions.filter { $0.status == .ok }
        if !completed.isEmpty {
            let details = completed.compactMap(\.message)
            appendAura(details.isEmpty ? "Done." : details.joined(separator: " "), failed: false)
            return
        }
        if let reply = activePlanReply, !reply.isEmpty {
            appendAura(reply, failed: false)
        } else {
            appendAura("That request finished. Check Activity for the result.", failed: false)
        }
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
        guard phase == "armed" else {
            toast("Aura is still working on the last request.", kind: .warning)
            return
        }

        activeSessionID = nil
        activePlanReply = nil
        liveActions.removeAll()
        appendUser(trimmed)
        phase = "planning"
        watchForStall()
        Task {
            do {
                let reply = try await client.send(text: trimmed)
                if reply.accepted == false {
                    let message = reply.message ?? "Aura is busy."
                    if let snapshot = try? await client.state(timeout: 1) {
                        state = snapshot
                        phase = snapshot.state
                    } else {
                        phase = "armed"
                    }
                    removeThinkingIndicator()
                    appendSystem(message)
                    toast(message, kind: .warning)
                }
            } catch let error as EngineError where error.isUnauthorized {
                removeThinkingIndicator()
                phase = "armed"
                handleUnauthorized()
            } catch {
                if let snapshot = try? await client.state(timeout: 1) {
                    state = snapshot
                    phase = snapshot.state
                } else {
                    phase = "armed"
                }
                appendAura(error.localizedDescription, failed: true)
            }
        }
    }

    func confirmProposal() {
        guard let proposal else { return }
        confirmProposal(token: proposal.id)
    }

    /// Resolve a proposal by token — from the panel card OR from the
    /// Run/Cancel buttons of the system notification.
    func confirmProposal(token: String) {
        guard !token.isEmpty else { return }
        proposal = nil
        notifiedProposalTokens.removeAll()
        Notify.clearProposals()
        Task {
            do {
                let reply = try await client.confirm(token: token)
                if reply.ok == false { toast("That request expired — ask again.", kind: .warning) }
            } catch {
                toast(error.localizedDescription, kind: .failure)
            }
        }
    }

    func cancelProposal() {
        guard let proposal else { return }
        cancelProposal(token: proposal.id)
    }

    func cancelProposal(token: String) {
        guard !token.isEmpty else { return }
        proposal = nil
        notifiedProposalTokens.removeAll()
        Notify.clearProposals()
        Task {
            do {
                let reply = try await client.cancel(token: token)
                toast(reply.ok ? "Cancelled — nothing ran." : "That request had already expired.",
                      kind: reply.ok ? .info : .warning)
            } catch {
                toast(error.localizedDescription, kind: .failure)
            }
        }
    }

    // MARK: - a pending question always reaches the user

    /// The engine's answer window for a proposal — the card counts down with
    /// it. The fallback matches config.default.toml when the config hasn't
    /// loaded yet.
    var confirmationTimeout: TimeInterval {
        config?.liveValue("session", "confirmation_timeout_seconds")?.doubleValue ?? 45
    }

    /// Deliver a pending question that the user cannot currently see.
    ///
    /// The confirmation card lives only inside the popover — one click
    /// elsewhere and a transient popover is gone, while the engine keeps
    /// waiting 45 s for an answer nobody can give any more. That silence is
    /// the bug this closes, so the question travels:
    ///   * panel visible → the card is on screen; nothing to do;
    ///   * panel hidden  → a system notification with Run / Cancel buttons
    ///                     (once per proposal — no re-bannering);
    ///   * notifications unavailable (denied — an ad-hoc rebuild orphans the
    ///     grant silently) → Aura presents its own panel instead.
    /// Every proposal now has a route to the user's eyes.
    func deliverProposalQuestion(_ pending: Proposal) {
        guard !pending.id.isEmpty, !Notify.panelIsVisible() else { return }
        guard !notifiedProposalTokens.contains(pending.id) else { return }
        notifiedProposalTokens.insert(pending.id)
        let titles = pending.actions.map { $0.title }.joined(separator: ", ")
        let title = "Aura needs your OK" + (titles.isEmpty ? "" : ": \(titles)")
        let body = pending.reply.isEmpty ? "Open Aura to review the plan." : pending.reply
        let token = pending.id
        Task { @MainActor [weak self] in
            if await Notify.ensureAuthorization() {
                Notify.proposal(title: title, body: body, token: token)
                self?.notificationsAllowed = true
            } else {
                self?.notificationsAllowed = false
                self?.log.write("Aura.app: notifications are off — presenting the panel "
                                + "so proposal \(token) is still seen")
                NotificationCenter.default.post(name: .auraOpenPanel, object: nil)
            }
        }
    }

    /// The popover just closed on its own (transient — any click elsewhere).
    /// A question still pending just lost its only visible home; deliver it.
    func panelDidClose() {
        guard let proposal else { return }
        deliverProposalQuestion(proposal)
    }

    /// Read macOS's live notification answer — for the Setup panel and for
    /// deciding whether a proposal alert can be trusted to show.
    func refreshNotificationStatus() async {
        notificationsAllowed = await Notify.refreshAuthorization()
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
        nativeAccessibilityGranted = Permissions.accessibilityGranted
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
    }

    func refreshConfig() async {
        config = try? await client.config()
        let snapshot = try? await client.state()
        if let snapshot { state = snapshot; phase = snapshot.state }
    }

    @discardableResult
    func refreshActivity() async -> Bool {
        do {
            activity = try await client.history()
            return true
        } catch {
            toast(error.localizedDescription, kind: .warning)
            return false
        }
    }

    func refreshPermissions() async {
        // Read Aura.app's TCC state before waiting for the engine, then again
        // after: the round trip takes long enough for a grant to land, and a
        // panel that shows a state the user has already changed is worse than
        // one extra read.
        nativeAccessibilityGranted = Permissions.accessibilityGranted
        permissions = try? await client.permissions()
        nativeAccessibilityGranted = Permissions.accessibilityGranted
        engineAccessibilityGranted = permissions?.accessibility
        engineAccessibilityDetail = permissions?.accessibilityDetail
        notificationsAllowed = await Notify.refreshAuthorization()
        let snapshot = try? await client.state()
        if let snapshot { state = snapshot }
    }

    private func refreshTraining() async {
        let snapshot = try? await client.training()
        if let snapshot { training = snapshot.active ? snapshot : nil }
    }

    /// A light re-read of the engine's live state — the Wake Phrase panel
    /// polls this while it is open so the listening meter stays honest.
    /// Touches `state` only: `phase` belongs to the SSE stream and the
    /// session code, and a poll must never fight them.
    func refreshState() async {
        if let snapshot = try? await client.state(timeout: 2) {
            state = snapshot
        }
    }

    // MARK: - settings

    func setTTS(_ enabled: Bool) {
        applyConfig(section: "tts", key: "enabled", value: .bool(enabled))
    }

    func setAskBeforeRun(_ enabled: Bool) {
        applyConfig(section: "safety", key: "show_plan_before_run", value: .bool(enabled))
    }

    func setWakeMode(_ mode: String) {
        guard !isChangingWakeMode else { return }
        isChangingWakeMode = true
        Task {
            defer { isChangingWakeMode = false }
            do {
                let reply = try await client.setWakeMode(mode)
                if reply.ok {
                    if mode == "openwakeword" {
                        toast("Always listening is active.", kind: .success)
                    }
                } else {
                    toast(reply.message ?? "Aura couldn't start that listening mode.",
                          kind: .warning)
                }
            } catch {
                toast(error.localizedDescription, kind: .warning)
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
            if target == "accessibility" {
                // Ask from the app itself: the native TCC prompt is the
                // reliable one, and macOS attributes the grant to Aura.
                Permissions.requestAccessibility()
            }
            do {
                // The engine may have started with SilentMic before the native
                // TCC answer. This call both verifies the device and hot-attaches
                // the real stream; no engine restart is required.
                let answer = try await client.requestPermission(target)
                if target == "accessibility" {
                    // The engine is the process that drives your apps, and it
                    // re-reads the grant from a fresh process — so its answer
                    // is the one to believe, and it names the app macOS filed
                    // the grant under. That is what turns "still not granted"
                    // into something the user can act on.
                    await refreshPermissions()
                    let granted = accessibilityStatus == true
                    let detail = accessibilityDetail ?? answer.message
                    if granted {
                        toast(detail ?? "Accessibility is granted to Aura.", kind: .success)
                    } else {
                        Permissions.openAccessibilitySettings()
                        toast(detail ?? "Switch Aura on in the Accessibility list — the System Settings pane is open.",
                              kind: .warning)
                    }
                } else {
                    toast(answer.message ?? "Checked.", kind: answer.ok ? .success : .warning)
                }
            } catch {
                if target == "accessibility" {
                    if accessibilityStatus == true {
                        toast("Accessibility is granted to Aura.", kind: .success)
                    } else {
                        Permissions.openAccessibilitySettings()
                        toast("Couldn't reach the engine — enable Aura in the Accessibility pane that just opened.",
                              kind: .warning)
                    }
                } else {
                    toast(error.localizedDescription, kind: .failure)
                }
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

    /// Notifications are the app's own permission — no engine round trip.
    /// macOS shows its prompt exactly once; after a "no" the only road back
    /// is System Settings, so a refused request opens the exact pane.
    func requestNotifications() {
        Task { @MainActor in
            let allowed = await Notify.ensureAuthorization()
            notificationsAllowed = allowed
            if allowed {
                toast("Aura can now alert you when the panel is closed.", kind: .success)
            } else {
                openNotificationSettings()
                toast("Turn Aura on in the Notifications pane that just opened.",
                      kind: .warning)
            }
        }
    }

    func openNotificationSettings() {
        let urlString = "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
        if let url = URL(string: urlString) {
            NSWorkspace.shared.open(url)
        }
    }

    func testAutomation() {
        guard !isTestingAutomation else { return }
        let probeID = UUID()
        automationProbeID = probeID
        isTestingAutomation = true
        automationTestMessage = "Testing a harmless AppleEvent to System Events…"
        automationTestSucceeded = nil
        toast("Checking Automation access…", kind: .info)

        Task { [weak self] in
            let result = await Task.detached(priority: .userInitiated) {
                Permissions.testAutomation()
            }.value
            guard let self, self.automationProbeID == probeID else { return }
            self.automationTimeoutTask?.cancel()
            self.automationTimeoutTask = nil
            self.automationProbeID = nil
            self.isTestingAutomation = false
            self.automationTestMessage = result.message
            self.automationTestSucceeded = result.ok
            if result.permissionRequired {
                Permissions.openAutomationSettings()
            }
            self.toast(result.message, kind: result.ok ? .success : .warning)
            await self.refreshPermissions()
        }

        // NSAppleScript can wait on a macOS consent sheet. Keep the UI honest
        // even if that sheet is behind another window; the row itself holds the
        // result, so the user does not have to catch a transient toast.
        automationTimeoutTask?.cancel()
        automationTimeoutTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 12_000_000_000)
            guard let self, self.automationProbeID == probeID else { return }
            self.automationProbeID = nil
            self.automationTimeoutTask = nil
            self.isTestingAutomation = false
            let message = "The test hasn't returned yet — check the macOS permission prompt or Privacy & Security › Automation."
            self.automationTestMessage = message
            self.automationTestSucceeded = false
            Permissions.openAutomationSettings()
            self.toast(message, kind: .warning)
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
                                        count: 0, need: start.need ?? 6, listening: false,
                                        message: "Ready — tap Record, then say your phrase.")
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
                                listening: true,
                                message: "Recording — say your phrase now, then pause briefly.")
        toast("Listening… say it now.", kind: .info)
        Task {
            do {
                let capture = try await client.captureSample()
                guard capture.ok else {
                    toast(capture.message ?? "Couldn't record that.", kind: .warning)
                    await refreshTraining()
                    return
                }

                // SSE is the fast path; this short poll is the recovery path if
                // the event stream reconnects while a take is in progress.
                for _ in 0..<12 {
                    guard training?.active == true,
                          training?.phrase == current.phrase,
                          training?.listening == true else { return }
                    try? await Task.sleep(nanoseconds: 500_000_000)
                    await refreshTraining()
                    if training?.listening != true { return }
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

    func feedbackStatus(for entryID: Int) -> ActivityFeedbackState? {
        feedbackByEntryID[entryID]
    }

    func feedback(_ entry: HistoryEntry, good: Bool) {
        let entryID = entry.id
        let vote: ActivityFeedbackState.Vote = good ? .good : .bad
        if let current = feedbackByEntryID[entryID],
           current.phase == .sending || current.phase == .sent {
            return
        }
        feedbackByEntryID[entryID] = ActivityFeedbackState(vote: vote, phase: .sending)

        // The verdict supervises the skill that actually ran, with the args
        // it ran with. A no-action session can still be noted, but must not be
        // described as a training example it cannot produce.
        let skill = entry.primarySkill ?? ""
        let args = entry.primaryArgs
        Task {
            do {
                let answer = try await client.feedback(transcript: entry.transcript,
                                                       skill: skill,
                                                       good: good,
                                                       args: args)
                guard answer.ok else {
                    feedbackByEntryID[entryID] = ActivityFeedbackState(vote: vote, phase: .failed)
                    toast(answer.message ?? "Aura couldn't save that feedback.", kind: .failure)
                    return
                }
                feedbackByEntryID[entryID] = ActivityFeedbackState(vote: vote, phase: .sent)
                let message: String
                if skill.isEmpty {
                    message = "Feedback sent — this session had no action for Aura to learn from."
                } else {
                    message = good ? "Thanks — feedback sent to Aura." : "Got it — correction sent to Aura."
                }
                toast(message, kind: .success)
            } catch {
                feedbackByEntryID[entryID] = ActivityFeedbackState(vote: vote, phase: .failed)
                toast(error.localizedDescription, kind: .failure)
            }
            _ = await refreshActivity()
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

    var isAlwaysListeningActive: Bool { state?.wakeActive == true }

    var wakeActivationPhrase: String? {
        guard isAlwaysListeningActive else { return nil }
        if let phrase = state?.wakePhrase?.trimmingCharacters(in: .whitespacesAndNewlines),
           !phrase.isEmpty { return phrase }
        guard let configured = config?.wakeModels?.first else { return nil }
        let name = URL(fileURLWithPath: configured).deletingPathExtension().lastPathComponent
        let known: [String: String] = [
            "hey_jarvis": "Hey Jarvis", "hey_mycroft": "Hey Mycroft",
            "hey_rhasspy": "Hey Rhasspy", "alexa": "Alexa", "okay_nabu": "Okay Nabu",
        ]
        return known[name] ?? name.replacingOccurrences(of: "_", with: " ").capitalized
    }

    var confidenceThreshold: Double {
        guard let value = config?.liveValue("laya", "confidence_threshold")?.doubleValue else { return 0.62 }
        return value
    }

    /// The Accessibility answer to show.
    ///
    /// The engine is the process that actually drives your apps, so its live
    /// reading wins when it has one — an engine started from a terminal is
    /// granted as that terminal, and only the engine can see that. Aura.app's
    /// own reading is the fallback for when the engine isn't answering yet.
    var accessibilityStatus: Bool? {
        engineAccessibilityGranted ?? nativeAccessibilityGranted
    }

    /// The engine's own sentence about the state, when it has one.
    var accessibilityDetail: String? {
        guard let detail = engineAccessibilityDetail, !detail.isEmpty else { return nil }
        return detail
    }

    var capabilities: [CapabilityRow] {
        let microphone = permissions?.microphone ?? state?.micReady
        let accessibility = accessibilityStatus
        let wakeReady = permissions?.wakeModels?.ready ?? false
        let alwaysOn = state?.wakeMode == "openwakeword"
        // Model files being present is not proof that the listener opened its
        // microphone or detector. For an enabled mode, only the engine's live
        // status can say whether Aura is actually listening.
        let wakeState = alwaysOn ? state?.wakeActive : wakeReady
        let wakeDetail = alwaysOn
            ? (state?.wakeDetail ?? state?.wakeError ?? "Checking the live wake listener…")
            : (permissions?.wakeModels?.detail ?? "Wake words come from the pretrained model set.")
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
                    ? (accessibilityDetail ?? "Aura can read and click inside your apps, as you ask.")
                    : (accessibilityDetail
                       ?? "Lets Aura see and click inside other apps. macOS asks you; Aura never guesses."),
                state: accessibility,
                action: accessibility == true ? .openAccessibility : .requestAccessibility,
                actionTitle: accessibility == true ? "Open System Settings" : "Grant access"),
            CapabilityRow(
                id: "automation", title: "Automation",
                detail: automationTestMessage ?? (isMac
                    ? "macOS asks per app the first time Aura drives it. This sends a harmless test to System Events."
                    : "AppleScript automation is a macOS feature."),
                state: automationTestSucceeded,
                action: .testAutomation,
                actionTitle: automationTestSucceeded == true ? "Run again" : "Run a test"),
            CapabilityRow(
                id: "notifications", title: "Notifications",
                detail: notificationsAllowed == true
                    ? "Confirmations reach you even when Aura's panel is closed — with Run and Cancel buttons."
                    : "Without notifications, a confirmation asked while the panel is closed has nowhere to go. Aura will pop its panel open instead — but alerts are the reliable route.",
                state: notificationsAllowed,
                action: notificationsAllowed == true
                    ? .openNotificationSettings : .requestNotifications,
                actionTitle: notificationsAllowed == true
                    ? "Open System Settings" : "Allow notifications"),
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
                state: wakeState,
                action: wakeReady ? .none : .installWakeModels,
                actionTitle: !wakeReady ? "Download models" :
                    (alwaysOn && wakeState == true ? "Listening" :
                     (alwaysOn ? "Check setup" : "Installed"))),
        ]
    }
}

extension Notification.Name {
    /// Posted when the global shortcut changes, so the hotkey can re-register.
    static let auraShortcutChanged = Notification.Name("AuraShortcutChanged")
    /// Posted when the panel/window should be shown (menu, shortcut, dock).
    static let auraOpenPanel = Notification.Name("AuraOpenPanel")
}
