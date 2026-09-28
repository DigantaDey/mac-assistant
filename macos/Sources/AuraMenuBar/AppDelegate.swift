import AppKit
import Carbon.HIToolbox
import ServiceManagement
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var webView: WKWebView!
    private var menu: NSMenu!
    private var loginItem: NSMenuItem!

    // First-launch / Setup window — a real NSWindow, the Apple-standard
    // place to have the consent conversation.
    private var setupWindow: NSWindow?

    private var python = PythonProcess()
    private lazy var monitor = ServerMonitor { [weak self] state in
        self?.apply(state)
    }
    private var lastState: ServerMonitor.State?

    private var port: Int {
        UserDefaults.standard.object(forKey: "port") as? Int ?? 7331
    }
    private var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildStatusItem()
        buildPopover()
        buildMenu()
        registerHotkey()

        apply(monitor.state)
        monitor.start()
        python.onStateChange = { [weak self] in self?.monitor.kick() }
        python.start()

        if !UserDefaults.standard.bool(forKey: "hasOnboarded") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                self?.showSetupWindow()
                self?.proactivePermissionAsks()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        python.stop()
    }

    // MARK: UI assembly

    private func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.image = NSImage(systemSymbolName: "dot.radiowaves.left.and.right",
                                   accessibilityDescription: "Aura")
            button.image?.isTemplate = true
            button.action = #selector(statusClicked)
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        statusItem = item
    }

    private func buildMenu() {
        let m = NSMenu()

        let open = NSMenuItem(title: "Open Aura",
                              action: #selector(openPanel), keyEquivalent: "o")
        open.target = self
        m.addItem(open)

        let wake = NSMenuItem(title: "Wake Aura  (⌥Space)",
                              action: #selector(wake), keyEquivalent: "")
        wake.target = self
        m.addItem(wake)

        let setup = NSMenuItem(title: "Setup & Permissions…",
                               action: #selector(openSetup), keyEquivalent: "s")
        setup.target = self
        m.addItem(setup)

        m.addItem(.separator())

        let browser = NSMenuItem(title: "Open in Browser",
                                 action: #selector(openInBrowser), keyEquivalent: "b")
        browser.target = self
        m.addItem(browser)

        let restart = NSMenuItem(title: "Restart Engine",
                                 action: #selector(restartEngine), keyEquivalent: "r")
        restart.target = self
        m.addItem(restart)

        m.addItem(.separator())

        let folder = NSMenuItem(title: "Choose Aura Folder…",
                                action: #selector(chooseFolder), keyEquivalent: "")
        folder.target = self
        m.addItem(folder)

        loginItem = NSMenuItem(title: "Start at Login",
                               action: #selector(toggleLogin), keyEquivalent: "")
        loginItem.target = self
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
        m.addItem(loginItem)

        let log = NSMenuItem(title: "Open Activity Log",
                             action: #selector(openLog), keyEquivalent: "")
        log.target = self
        m.addItem(log)

        m.addItem(.separator())

        let quit = NSMenuItem(title: "Quit Aura",
                              action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        m.addItem(quit)

        menu = m
    }

    private func buildPopover() {
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 480, height: 680),
                            configuration: WKWebViewConfiguration())
        web.underPageBackgroundColor = .clear
        web.setValue(false, forKey: "drawsBackground")   // popover vibrancy shows through
        webView = web

        let controller = NSViewController()
        controller.view = web
        let panel = NSPopover()
        panel.contentViewController = controller
        panel.behavior = .transient
        panel.appearance = NSAppearance(named: .darkAqua)
        panel.contentSize = NSSize(width: 480, height: 680)
        popover = panel
        loadPanel(onboarding: false)
    }

    // MARK: server readiness gate — never show a blank webview

    private func placeholderHTML(_ title: String, _ body: String) -> String {
        """
        <html><head><meta charset="utf-8"></head>
        <body style="margin:0;height:100vh;display:flex;flex-direction:column;
          align-items:center;justify-content:center;gap:14px;
          background:rgba(12,13,16,0.92);
          font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
          -webkit-font-smoothing:antialiased;">
          <div style="width:12px;height:12px;border-radius:50%;background:#0a84ff;
            animation:p 1.2s ease-in-out infinite;"></div>
          <div style="color:#f5f5f7;font-size:14px;font-weight:600;">\(title)</div>
          <div style="color:rgba(235,235,245,0.55);font-size:12px;max-width:320px;
            text-align:center;line-height:1.5;">\(body)</div>
          <style>@keyframes p{0%,100%{opacity:.35;transform:scale(.8)}50%{opacity:1;transform:scale(1.15)}}</style>
        </body></html>
        """
    }

    private func waitThenLoad(_ web: WKWebView, url: URL, onboarding: Bool) {
        web.loadHTMLString(placeholderHTML("Waking Aura…",
            "The engine starts in a second. If this lasts a while, choose Restart Engine from the menu."),
            baseURL: nil)
        var tries = 0
        func poll() {
            tries += 1
            var request = URLRequest(url: url.appendingPathComponent("api/health"))
            request.timeoutInterval = 1.2
            URLSession.shared.dataTask(with: request) { data, response, _ in
                let up = (response as? HTTPURLResponse)?.statusCode == 200
                DispatchQueue.main.async {
                    if up {
                        var target = self.baseURL
                        if onboarding { target.append(fragment: "onboarding") }
                        web.load(URLRequest(url: target))
                    } else if tries < 40 {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { poll() }
                    } else {
                        web.loadHTMLString(placeholderHTML("Aura's engine is offline",
                            "Pick Restart Engine from the menu bar, or check that the engine folder is still in place."),
                            baseURL: nil)
                    }
                }
            }.resume()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { poll() }
    }

    private func loadPanel(onboarding: Bool) {
        waitThenLoad(webView, url: baseURL, onboarding: onboarding)
    }

    // MARK: first-launch window + proactive permission conversation

    @objc private func openSetup() {
        showSetupWindow()
    }

    private func showSetupWindow() {
        if setupWindow != nil {
            setupWindow?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 720, height: 700),
                            configuration: WKWebViewConfiguration())
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 700),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered, defer: false)
        window.title = "Welcome to Aura"
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(calibratedRed: 0.047, green: 0.051, blue: 0.063, alpha: 1)
        window.isReleasedWhenClosed = false
        window.contentView = web
        window.center()
        window.delegate = SetupWindowDelegate(delegate: self)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        setupWindow = window
        waitThenLoad(web, url: baseURL, onboarding: true)
    }

    func setupWindowDidClose() {
        setupWindow?.close()
        setupWindow = nil
        UserDefaults.standard.set(true, forKey: "hasOnboarded")
    }

    /// The consent conversation, on the app's own terms: microphone first
    /// (its dialog carries our usage string), then Accessibility (system
    /// sheet with an Open System Settings button). Neither is nagging —
    /// each happens once, and the Setup window shows the honest state after.
    private func proactivePermissionAsks() {
        Task { @MainActor in
            await Permissions.requestMicrophone()
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            Permissions.requestAccessibility()
        }
    }

    // MARK: actions

    @objc private func statusClicked() {
        guard let event = NSApp.currentEvent else { togglePopover(); return }
        if event.type == .rightMouseUp || event.type == .otherMouseUp {
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            togglePopover()
        }
    }

    private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    @objc private func openPanel() {
        if !popover.isShown { togglePopover() }
    }

    @objc private func wake() {
        openPanel()
        var request = URLRequest(url: baseURL.appendingPathComponent("api/trigger"))
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request).resume()
    }

    @objc private func openInBrowser() {
        NSWorkspace.shared.open(baseURL)
    }

    @objc private func restartEngine() {
        python.restart()
    }

    @objc private func chooseFolder() {
        UserDefaults.standard.removeObject(forKey: "repoPath")
        python.restart()
    }

    @objc private func toggleLogin() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled {
                try service.unregister()
            } else {
                try service.register()
            }
        } catch {
            NSLog("Aura login item error: \(error.localizedDescription)")
        }
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
    }

    @objc private func openLog() {
        NSWorkspace.shared.open(PythonProcess.logURL)
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    // MARK: state → icon

    private func apply(_ state: ServerMonitor.State) {
        let previous = lastState
        lastState = state
        guard let button = statusItem?.button else { return }
        switch state {
        case .armed:
            button.image = NSImage(systemSymbolName: "dot.radiowaves.left.and.right",
                                   accessibilityDescription: "Aura ready")
            button.contentTintColor = nil
        case .busy:
            button.image = NSImage(systemSymbolName: "waveform",
                                   accessibilityDescription: "Aura working")
            button.contentTintColor = .controlAccentColor
        case .attention:
            button.image = NSImage(systemSymbolName: "exclamationmark.circle.fill",
                                   accessibilityDescription: "Aura needs your OK")
            button.contentTintColor = .systemOrange
        case .down:
            button.image = NSImage(systemSymbolName: "exclamationmark.triangle",
                                   accessibilityDescription: "Aura offline")
            button.contentTintColor = .systemRed
        }
        button.image?.isTemplate = true
        if previous == .down, state != .down {
            loadPanel(onboarding: false)   // server recovered — reload the UI
        }
    }

    // MARK: global hotkey (⌥Space) — Carbon registration, no permission needed

    private func registerHotkey() {
        var hotKeyID = EventHotKeyID(signature: fourCC("AURA"), id: 1)
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        let handler: EventHandlerUPP = { _, eventRef, userData in
            guard let eventRef = eventRef, let userData = userData else { return noErr }
            guard GetEventKind(eventRef) == UInt32(kEventHotKeyPressed) else { return noErr }
            let delegate = Unmanaged<AppDelegate>.fromOpaque(userData).takeUnretainedValue()
            DispatchQueue.main.async { delegate.wake() }
            return noErr
        }
        InstallApplicationEventHandler(handler, 1, [eventType],
                                       Unmanaged.passUnretained(self).toOpaque(), nil)
        RegisterEventHotKey(UInt32(kVK_Space), UInt32(optionKey), hotKeyID,
                            GetApplicationEventTarget(), 0, nil)
    }

    private func fourCC(_ string: String) -> OSType {
        var value: OSType = 0
        for byte in string.utf8.prefix(4) {
            value = (value << 8) | OSType(byte)
        }
        return value
    }
}

// MARK: - small delegates

final class SetupWindowDelegate: NSObject, NSWindowDelegate {
    private let delegate: AppDelegate
    init(delegate: AppDelegate) { self.delegate = delegate }
    func windowWillClose(_ notification: Notification) {
        delegate.setupWindowDidClose()
    }
}
