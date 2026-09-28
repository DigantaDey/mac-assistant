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

        let firstRun = !UserDefaults.standard.bool(forKey: "hasOnboarded")
        loadPanel(onboarding: firstRun)
        UserDefaults.standard.set(true, forKey: "hasOnboarded")
    }

    private func loadPanel(onboarding: Bool) {
        var url = baseURL
        if onboarding { url.append(fragment: "onboarding") }
        webView.load(URLRequest(url: url))
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
