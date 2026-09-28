import AppKit
import Darwin

// Owns the Python orchestrator process: launch, log, restart with backoff,
// clean shutdown. GUI apps don't inherit your shell PATH, so Homebrew's bins
// are appended explicitly — that's where whisper/ollama usually live.

final class PythonProcess {

    var onStateChange: (() -> Void)?

    private var process: Process?
    private var restartTimer: Timer?
    private var intentionalStop = false
    private var processStartedAt = Date.distantPast
    // Bounded auto-restart: an engine that dies on startup (missing venv,
    // wrong Python) must not respawn every 3 s forever — back off to 2 min
    // and stop trying after 8 straight failures. "Restart Engine" always works.
    private var consecutiveFailures = 0
    private let maxConsecutiveFailures = 8

    static var logURL: URL {
        let logs = FileManager.default
            .urls(for: .libraryDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        return logs.appendingPathComponent("Aura.log")
    }

    // MARK: lifecycle

    func start() {
        intentionalStop = false
        consecutiveFailures = 0
        spawn()
    }

    func stop() {
        intentionalStop = true
        restartTimer?.invalidate()
        restartTimer = nil
        guard let process = process, process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
    }

    func restart() {
        stop()
        start()
    }

    /// Menu → "Choose Aura Folder…": forget the remembered folder, ask for a
    /// new one, relaunch. Cancelling leaves the standard discovery in place.
    func chooseRepo() {
        UserDefaults.standard.removeObject(forKey: "repoPath")
        guard let picked = Self.pickRepo() else { return }
        UserDefaults.standard.set(picked, forKey: "repoPath")
        restart()
    }

    // MARK: internals

    private func spawn() {
        let defaults = UserDefaults.standard
        let fm = FileManager.default

        // Engine discovery — no file picker on a normal install:
        //   1. the folder a user explicitly chose (menu → "Choose Aura Folder…")
        //   2. the standard installed copy the installer places in
        //      ~/Library/Application Support/Aura/engine
        //   3. last resort: ask the user to pick the mac-assistant folder
        var repoPath = defaults.string(forKey: "repoPath") ?? ""
        if repoPath.isEmpty || !fm.fileExists(atPath: repoPath + "/aura") {
            let installed = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Aura/engine", isDirectory: true)
            if fm.fileExists(atPath: installed.appendingPathComponent("aura").path) {
                repoPath = installed.path
                defaults.set(repoPath, forKey: "repoPath")
            } else {
                guard let picked = Self.pickRepo() else {
                    onStateChange?()          // stayed down; user can retry from the menu
                    return
                }
                repoPath = picked
                defaults.set(picked, forKey: "repoPath")
            }
        }

        let repo = URL(fileURLWithPath: repoPath, isDirectory: true)
        let venvPython = repo.appendingPathComponent(".venv/bin/python")
        let python = fm.fileExists(atPath: venvPython.path)
            ? venvPython
            : URL(fileURLWithPath: "/usr/bin/python3")

        let child = Process()
        child.executableURL = python
        child.arguments = ["-m", "aura", "serve"]
        child.currentDirectoryURL = repo

        var environment = ProcessInfo.processInfo.environment
        let brewPaths = "/opt/homebrew/bin:/usr/local/bin"
        environment["PATH"] = (environment["PATH"].map { $0 + ":" + brewPaths }) ?? brewPaths
        let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Aura", isDirectory: true)
        try? fm.createDirectory(at: support, withIntermediateDirectories: true)
        environment["AURA_DATA_DIR"] = support.path
        environment["AURA_ENGINE_PATH"] = repo.path   // the engine knows its own home
        environment["PYTHONUNBUFFERED"] = "1"
        child.environment = environment

        // Log to ~/Library/Logs/Aura.log, truncating at ~5 MB.
        let logURL = Self.logURL
        if let attributes = try? fm.attributesOfItem(atPath: logURL.path),
           let size = attributes[.size] as? UInt64, size > 5_000_000 {
            try? fm.removeItem(at: logURL)
        }
        if !fm.fileExists(atPath: logURL.path) {
            fm.createFile(atPath: logURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            _ = try? handle.seekToEnd()
            child.standardOutput = handle
            child.standardError = handle
        }

        child.terminationHandler = { [weak self] _ in
            guard let self = self else { return }
            self.onStateChange?()
            guard !self.intentionalStop else { return }
            // A process that lived a while isn't a startup failure — reset the
            // backoff so a crash after hours restarts promptly.
            if Date().timeIntervalSince(self.processStartedAt) > 60 {
                self.consecutiveFailures = 0
            }
            self.consecutiveFailures += 1
            guard self.consecutiveFailures <= self.maxConsecutiveFailures else {
                NSLog("Aura: engine has failed \(self.consecutiveFailures) times in a row — " +
                      "stopping auto-restart. Pick “Restart Engine” from the menu bar " +
                      "(or check ~/Library/Logs/Aura.log).")
                return
            }
            let delay = min(3.0 * pow(2.0, Double(self.consecutiveFailures - 1)), 120.0)
            NSLog("Aura: engine exited — restarting in \(Int(delay)) s (failure \(self.consecutiveFailures)/\(self.maxConsecutiveFailures))")
            DispatchQueue.main.async { [weak self] in
                self?.restartTimer = Timer.scheduledTimer(withTimeInterval: delay,
                                                          repeats: false) { [weak self] _ in
                    self?.spawn()
                }
            }
        }

        do {
            try child.run()
            process = child
            processStartedAt = Date()
        } catch {
            NSLog("Aura: could not launch \(python.path): \(error.localizedDescription)")
        }
        onStateChange?()
    }

    private static func pickRepo() -> String? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Select your mac-assistant folder (the one containing the aura/ package)"
        panel.prompt = "Use folder"
        guard panel.runModal() == .OK else { return nil }
        return panel.url?.path
    }
}
