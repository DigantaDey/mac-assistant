import Foundation

/// Owns Aura's engine process: finds it, launches it, keeps it alive, and
/// says — in plain language — what is wrong when it can't.
///
/// Rules it follows, because getting these wrong is what makes a menu-bar app
/// feel broken:
///   * never spawn a second engine if a healthy one is already on the port;
///   * never fight another app for the port — say whose port it is;
///   * never restart forever: back off, then stop *and tell the user*;
///   * never leave the child behind when the app quits.
@MainActor
public final class EngineSupervisor {

    public enum Status: Equatable {
        case stopped
        case starting
        case running(external: Bool)
        case failed(String)

        public var isRunning: Bool {
            if case .running = self { return true }
            return false
        }

        public var label: String {
            switch self {
            case .stopped: return "Stopped"
            case .starting: return "Starting…"
            case .running(let external): return external ? "Running (already open)" : "Running"
            case .failed: return "Needs attention"
            }
        }

        public var detail: String? {
            if case .failed(let message) = self { return message }
            return nil
        }
    }

    public private(set) var status: Status = .stopped {
        didSet {
            guard status != oldValue else { return }
            onStatusChange?(status)
        }
    }

    public var onStatusChange: ((Status) -> Void)?
    /// Engine output the app wants to surface (rare, but useful for setup).
    public var onLog: ((String) -> Void)?
    /// The app supplies a folder picker; called only when discovery fails.
    public var locateEngine: (@MainActor () -> String?)?

    public private(set) var engineDirectory: URL?

    private let client: EngineClient
    private let token: String
    private let log: AuraLog
    private var process: Process?
    private var watchdog: Timer?
    private var restartAttempts = 0
    private var launchedAt = Date.distantPast
    private var intentionalStop = false
    /// Serialises asynchronous boot requests. A termination callback, app
    /// activation and a manual restart can otherwise all pass the health probe
    /// before any child binds the port, spawning two engines.
    private var bootInProgress = false
    private let maxRestartAttempts = 8

    public init(client: EngineClient, token: String, log: AuraLog = .shared) {
        self.client = client
        self.token = token
        self.log = log
    }

    // MARK: - lifecycle

    public func start() {
        intentionalStop = false
        restartAttempts = 0
        Task { await boot() }
    }

    public func stop() {
        intentionalStop = true
        watchdog?.invalidate()
        watchdog = nil
        guard let process, process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(2.5)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
        self.process = nil
        status = .stopped
    }

    public func restart() {
        stop()
        start()
    }

    /// Menu → "Choose Engine Folder…"
    public func chooseEngineFolder() {
        guard let picked = locateEngine?() else { return }
        guard AppPaths.looksLikeEngine(picked) else {
            status = .failed("That folder doesn't contain Aura's engine (no aura/ package found).")
            return
        }
        Prefs.enginePath = picked
        log.write("Aura.app: engine folder set to \(picked)")
        restart()
    }

    public func clearEngineFolder() {
        Prefs.enginePath = nil
    }

    // MARK: - boot sequence

    private func boot() async {
        guard !bootInProgress else {
            log.write("Aura.app: ignored duplicate engine start while boot is in progress")
            return
        }
        bootInProgress = true
        defer { bootInProgress = false }

        status = .starting
        AppPaths.ensureSupportDirectory()

        // 1 — an engine may already be running (app relaunch, or a second copy
        //     of the app). Adopt only the same engine version. Attaching v0.6.2
        //     UI to a months-old v0.6.0 process leaves the newly installed code
        //     unused and was the reason upgrades appeared to do nothing.
        if let health = await probe() {
            if health.version == AuraVersion.semantic {
                status = .running(external: true)
                log.write("Aura.app: attached to a running engine (v\(health.version ?? "?"))")
                startWatchdog()
                return
            }
            let found = health.version ?? "unknown"
            log.write("Aura.app: running engine v\(found) does not match app v\(AuraVersion.semantic); replacing it")
            if let occupant = await portOccupant(), occupant.isAuraEngine {
                occupant.terminate()
                try? await Task.sleep(nanoseconds: 900_000_000)
            } else {
                status = .failed("Aura engine v\(found) is still using port \(Prefs.port). Quit it and restart Aura.")
                return
            }
        }

        // 2 — something else holds the port. If it's an old Aura engine with a
        //     different key, reclaim it; if it's another app, say so.
        if let occupant = await portOccupant() {
            if occupant.isAuraEngine {
                log.write("Aura.app: reclaiming port \(Prefs.port) from a stale Aura engine (pid \(occupant.pid))")
                occupant.terminate()
                try? await Task.sleep(nanoseconds: 900_000_000)
            } else {
                status = .failed("Port \(Prefs.port) is used by “\(occupant.command)”. "
                                 + "Quit that app, or set another port in config.toml.")
                return
            }
        }

        // 3 — find the engine on disk.
        guard let directory = resolveEngineDirectory() else {
            status = .failed("Aura's engine folder wasn't found. Use “Choose Engine Folder…” "
                             + "to point Aura at the mac-assistant checkout.")
            return
        }
        engineDirectory = directory
        spawn(directory: directory)

        // 4 — wait for it to answer, so the UI never shows a dead window.
        if await awaitHealthy(seconds: 25) {
            status = .running(external: false)
            restartAttempts = 0
        } else {
            status = .failed(engineFailureHint())
            return
        }
        startWatchdog()
    }

    private func engineFailureHint() -> String {
        if let process, !process.isRunning {
            return "Aura's engine stopped right after starting. The log has the reason "
                 + "(Activity ▸ Open Log)."
        }
        return "Aura's engine is taking too long to answer. Try “Restart Engine” in the menu."
    }

    // MARK: - discovery

    private func resolveEngineDirectory() -> URL? {
        var candidates: [String] = []
        if let remembered = Prefs.enginePath { candidates.append(remembered) }
        if let fromEnvironment = ProcessInfo.processInfo.environment["AURA_ENGINE_PATH"] {
            candidates.append(fromEnvironment)
        }
        candidates.append(AppPaths.installedEngineDirectory.path)

        for path in candidates where AppPaths.looksLikeEngine(path) {
            if Prefs.enginePath != path {
                Prefs.enginePath = path
            }
            return URL(fileURLWithPath: path, isDirectory: true)
        }

        // Nothing on disk that we know about — ask the user, once.
        guard let picked = locateEngine?(), AppPaths.looksLikeEngine(picked) else { return nil }
        Prefs.enginePath = picked
        return URL(fileURLWithPath: picked, isDirectory: true)
    }

    /// A Python that can run the engine, preferring the venv the installer made.
    private func pythonExecutable(in directory: URL) -> URL? {
        let candidates = [
            directory.appendingPathComponent(".venv/bin/python").path,
            "/opt/homebrew/bin/python3.12",
            "/opt/homebrew/bin/python3.11",
            "/usr/local/bin/python3.12",
            "/usr/local/bin/python3.11",
            "/usr/bin/python3",
        ]
        let manager = FileManager.default
        for path in candidates where manager.isExecutableFile(atPath: path) {
            return URL(fileURLWithPath: path)
        }
        return nil
    }

    // MARK: - spawning

    private func spawn(directory: URL) {
        guard let python = pythonExecutable(in: directory) else {
            status = .failed("Aura needs Python 3.11 or newer. Run scripts/install.sh again "
                             + "to set up the engine environment.")
            return
        }

        let child = Process()
        child.executableURL = python
        child.arguments = ["-m", "aura", "serve"]
        child.currentDirectoryURL = directory

        var environment = ProcessInfo.processInfo.environment
        let brewPaths = "/opt/homebrew/bin:/usr/local/bin"
        environment["PATH"] = environment["PATH"].map { "\(brewPaths):\($0)" } ?? brewPaths
        environment["AURA_DATA_DIR"] = AppPaths.dataDirectory.path
        environment["AURA_ENGINE_PATH"] = directory.path
        environment["AURA_TOKEN"] = token
        environment["AURA_PORT"] = String(Prefs.port)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        child.environment = environment

        if let handle = log.childHandle() {
            child.standardOutput = handle
            child.standardError = handle
        }

        child.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            Task { @MainActor [weak self] in
                self?.handleExit(code: status)
            }
        }

        do {
            try child.run()
            process = child
            launchedAt = Date()
            log.write("Aura.app: started engine \(python.path) -m aura serve (pid \(child.processIdentifier))")
        } catch {
            status = .failed("Couldn't start Aura's engine: \(error.localizedDescription)")
            log.write("Aura.app: engine launch failed — \(error.localizedDescription)")
        }
    }

    private func handleExit(code: Int32) {
        process = nil
        guard !intentionalStop else { return }
        log.write("Aura.app: engine exited (code \(code))")

        // A process that ran a while isn't a startup failure — restart promptly.
        if Date().timeIntervalSince(launchedAt) > 60 {
            restartAttempts = 0
        }
        restartAttempts += 1

        guard restartAttempts <= maxRestartAttempts else {
            status = .failed("Aura's engine keeps stopping. Open the log (Activity ▸ Open Log), "
                             + "then use “Restart Engine” when you're ready.")
            return
        }

        let delay = min(3.0 * pow(2.0, Double(restartAttempts - 1)), 120.0)
        status = .starting
        log.write("Aura.app: restarting the engine in \(Int(delay))s "
                  + "(attempt \(restartAttempts)/\(maxRestartAttempts))")
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard let self, !self.intentionalStop else { return }
            await self.boot()
        }
    }

    // MARK: - health

    /// One probe. `nil` = nothing healthy on the port (including 401/403).
    public func probe() async -> EngineHealth? {
        do {
            return try await client.health(timeout: 2)
        } catch {
            return nil
        }
    }

    private func awaitHealthy(seconds: Double) async -> Bool {
        let attempts = Int(seconds / 0.5)
        for _ in 0..<max(attempts, 1) {
            if process?.isRunning == false { return false }
            if await probe() != nil { return true }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        return false
    }

    private func startWatchdog() {
        watchdog?.invalidate()
        let timer = Timer.scheduledTimer(withTimeInterval: 4.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                _ = await self.probe()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        watchdog = timer
    }

    // MARK: - port occupancy

    public struct PortOccupant {
        public let pid: Int32
        public let command: String
        public var isAuraEngine: Bool {
            let lowered = command.lowercased()
            return lowered.contains("aura") || lowered.contains("python")
        }
        public func terminate() {
            kill(pid, SIGTERM)
        }
    }

    /// Who is listening on Aura's port? Uses `lsof`, which ships with macOS.
    public func portOccupant() async -> PortOccupant? {
        let output = await runProcess("/usr/sbin/lsof", ["-nP", "-iTCP:\(Prefs.port)", "-sTCP:LISTEN", "-t"])
        let pids = output.split(whereSeparator: \.isNewline)
            .compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
            .filter { $0 > 0 && $0 != ProcessInfo.processInfo.processIdentifier }
        guard let pid = pids.first else { return nil }
        let commandOutput = await runProcess("/bin/ps", ["-p", String(pid), "-o", "command="])
        let command = commandOutput.trimmingCharacters(in: .whitespacesAndNewlines)
        return PortOccupant(pid: pid, command: command.isEmpty ? "another process" : command)
    }

    private func runProcess(_ launchPath: String, _ arguments: [String]) async -> String {
        guard FileManager.default.isExecutableFile(atPath: launchPath) else { return "" }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: launchPath)
        task.arguments = arguments
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()
        do {
            try task.run()
        } catch {
            return ""
        }
        let data = (try? pipe.fileHandleForReading.readToEnd()) ?? Data()
        task.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? ""
    }
}
