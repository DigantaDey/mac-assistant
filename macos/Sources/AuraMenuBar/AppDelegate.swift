import AppKit
import AuraCore
import Combine
import SwiftUI

/// The menu-bar app: one icon, one popover, a few windows, and the engine
/// behind them. No web view, no Dock icon, no browser anywhere.
///
/// Every AppKit callback and every model interaction happens on the main
/// actor — saying so in the type system is what keeps the compiler (and the
/// reader) honest about it.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    /// Created from `main.swift` before the run loop exists, hence nonisolated.
    nonisolated override init() { super.init() }

    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private var menu: NSMenu!
    private var hotKey: GlobalHotKey?
    private var model: AppModel!
    private var onboardingWindow: NSWindow?
    private var settingsWindow: NSWindow?
    private var cancellables = Set<AnyCancellable>()

    // MARK: - lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        let token = AccessToken.loadOrCreate()
        model = AppModel(token: token)

        buildStatusItem()
        buildPopover()
        buildMenu()
        observe()
        registerHotKey()
        observeNotifications()

        model.start()

        if !Prefs.hasOnboarded {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { [weak self] in
                self?.showOnboarding()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        model?.shutdown()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false   // Aura lives in the menu bar
    }

    // MARK: - assembly

    private func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.action = #selector(statusItemClicked)
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.imagePosition = .imageOnly
        }
        statusItem = item
        updateIcon()
    }

    private func buildPopover() {
        let hosting = NSHostingController(rootView: PanelView().environmentObject(model))
        hosting.view.frame = NSRect(x: 0, y: 0, width: Theme.panelWidth, height: Theme.panelHeight)
        popover.contentViewController = hosting
        popover.contentSize = NSSize(width: Theme.panelWidth, height: Theme.panelHeight)
        popover.behavior = .transient
        popover.animates = true
    }

    private func buildMenu() {
        let menu = NSMenu()

        menu.addItem(item("Open Aura", #selector(openPanel), key: "o"))
        menu.addItem(item("Wake Aura", #selector(wake)))
        menu.addItem(.separator())

        menu.addItem(item("Settings…", #selector(openSettings), key: ","))
        menu.addItem(item("Setup & Permissions…", #selector(openSetup), key: "s"))
        menu.addItem(item("Activity…", #selector(openActivity), key: "a"))
        menu.addItem(.separator())

        menu.addItem(item("Restart Engine", #selector(restartEngine), key: "r"))
        menu.addItem(item("Choose Engine Folder…", #selector(chooseFolder)))
        menu.addItem(item("Start at Login", #selector(toggleLogin)))
        menu.addItem(.separator())

        menu.addItem(item("Open Data Folder", #selector(openDataFolder)))
        menu.addItem(item("Open Log", #selector(openLog), key: "l"))
        menu.addItem(.separator())
        menu.addItem(item("Quit Aura", #selector(quit), key: "q"))

        self.menu = menu
    }

    /// Every menu item is wired to this delegate — the classic reason a
    /// generated menu does nothing is a missing target.
    private func item(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    private func observe() {
        model.$phase
            .sink { [weak self] _ in self?.updateIcon() }
            .store(in: &cancellables)
        model.$engineStatus
            .sink { [weak self] _ in self?.updateIcon() }
            .store(in: &cancellables)
    }

    private func observeNotifications() {
        NotificationCenter.default.addObserver(forName: .auraOpenPanel, object: nil,
                                               queue: .main) { [weak self] note in
            guard let self else { return }
            guard let target = note.object as? String else {
                self.togglePopover()
                return
            }
            if target.hasPrefix("settings") || target == "activity" {
                self.showSettings(section: target)
            } else {
                self.togglePopover()
            }
        }

        NotificationCenter.default.addObserver(forName: .auraShortcutChanged, object: nil,
                                               queue: .main) { [weak self] _ in
            self?.registerHotKey()
        }
    }

    // MARK: - the icon

    private var phase: String { model?.phase ?? "armed" }

    private func updateIcon() {
        guard let button = statusItem?.button else { return }
        let symbol: String
        var tint: NSColor?

        switch model.engineStatus {
        case .failed:
            symbol = "exclamationmark.triangle.fill"
            tint = .systemRed
        case .stopped:
            symbol = "moon.zzz.fill"
            tint = .secondaryLabelColor
        case .starting:
            symbol = "arrow.triangle.2.circlepath"
            tint = .secondaryLabelColor
        case .running:
            switch phase {
            case "capturing":
                symbol = "waveform"
                tint = .controlAccentColor
            case "proposing":
                symbol = "shield.lefthalf.filled"
                tint = .systemOrange
            case "transcribing", "planning", "executing", "responding":
                symbol = "ellipsis.circle"
                tint = .controlAccentColor
            case "disabled":
                symbol = "moon.zzz.fill"
                tint = .secondaryLabelColor
            default:
                symbol = "dot.radiowaves.left.and.right"
                tint = nil
            }
        }

        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Aura")
        image?.isTemplate = true
        button.image = image
        button.contentTintColor = tint
        button.toolTip = "Aura — \(model.phaseLabel)"
    }

    // MARK: - interactions

    @objc private func statusItemClicked() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp, let button = statusItem.button {
            updateMenuState()
            _ = menu.popUp(positioning: nil,
                           at: NSPoint(x: 0, y: button.bounds.height + 4),
                           in: button)
        } else {
            togglePopover()
        }
    }

    private func updateMenuState() {
        for item in menu.items {
            if item.action == #selector(toggleLogin) {
                item.state = LaunchAtLogin.isEnabled ? .on : .off
            }
        }
    }

    private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        Task { await model.refreshAll() }
    }

    // MARK: - windows

    private func showOnboarding() {
        if let window = onboardingWindow {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let view = OnboardingView(onFinish: { [weak self] in self?.closeOnboarding() })
            .environmentObject(model)
        let controller = NSHostingController(rootView: view)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 540),
                              styleMask: [.titled, .closable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.title = "Welcome to Aura"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isReleasedWhenClosed = false
        window.contentViewController = controller
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        onboardingWindow = window
    }

    private func closeOnboarding() {
        onboardingWindow?.close()
        onboardingWindow = nil
        Prefs.hasOnboarded = true
    }

    private func showSettings(section: String? = nil) {
        if let section { model.requestedSection = section }
        if settingsWindow == nil {
            let controller = NSHostingController(rootView: SettingsView().environmentObject(model))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 620),
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable],
                                  backing: .buffered, defer: false)
            window.title = "Aura Settings"
            window.isReleasedWhenClosed = false
            window.contentViewController = controller
            window.setContentSize(NSSize(width: 780, height: 620))
            window.center()
            settingsWindow = window
        }
        settingsWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - menu actions

    @objc private func openPanel() { if !popover.isShown { togglePopover() } }

    @objc private func wake() {
        if Prefs.showPanelOnWake { openPanel() }
        model.wake()
    }

    @objc private func openSettings() { showSettings(section: "settings") }
    @objc private func openSetup() { showSettings(section: "settings/setup") }
    @objc private func openActivity() { showSettings(section: "activity") }
    @objc private func restartEngine() { model.restartEngine() }
    @objc private func chooseFolder() { model.chooseEngineFolder() }
    @objc private func openDataFolder() { model.openDataFolder() }
    @objc private func openLog() { model.openLogFolder() }

    @objc private func toggleLogin() {
        let enable = !LaunchAtLogin.isEnabled
        if let message = LaunchAtLogin.set(enabled: enable) {
            model.toast(message, kind: .warning)
        }
        updateMenuState()
    }

    @objc private func quit() { NSApp.terminate(nil) }

    // MARK: - the global shortcut

    private func registerHotKey() {
        hotKey?.unregister()
        hotKey = nil
        let shortcut = Prefs.wakeShortcut
        guard shortcut != .none else { return }
        hotKey = GlobalHotKey(shortcut: shortcut) { [weak self] in
            self?.wake()
        }
    }
}
