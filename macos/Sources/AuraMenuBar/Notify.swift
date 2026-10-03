import Foundation
import UserNotifications

/// Aura's reach beyond the panel: one system notification.
///
/// The confirmation card is the product's safety contract — but a menu-bar
/// popover is transient: click anywhere else and it closes, taking the
/// "Empty the trash — go ahead?" card with it. The engine then waits 45
/// seconds for an answer that never comes and cancels in silence. That used
/// to read as "nothing happened". Now every proposal also arrives as a real
/// macOS notification with Run / Cancel buttons, and answering it resolves
/// the exact same engine token the panel card would.
enum Notify {

    static let proposalCategory = "aura.proposal"
    static let runAction = "aura.proposal.run"
    static let cancelAction = "aura.proposal.cancel"

    /// Wired by the AppDelegate once the model exists.
    @MainActor static var onRun: (String) -> Void = { _ in }
    @MainActor static var onCancel: (String) -> Void = { _ in }
    @MainActor static var onShow: () -> Void = {}
    /// Whether the panel is on screen right now — the proposal card is
    /// visible there, so the notification is only needed when it isn't.
    @MainActor static var panelIsVisible: () -> Bool = { true }

    // MARK: - setup

    static func bootstrap() {
        let center = UNUserNotificationCenter.current()
        center.delegate = Delegate.shared

        let run = UNNotificationAction(identifier: runAction,
                                       title: "Run it",
                                       options: [])
        let cancel = UNNotificationAction(identifier: cancelAction,
                                          title: "Cancel",
                                          options: [.destructive])
        let category = UNNotificationCategory(identifier: proposalCategory,
                                              actions: [run, cancel],
                                              intentIdentifiers: [],
                                              options: [])
        center.setNotificationCategories([category])

        // Best effort: an LSUIElement app asks once; if the user said no,
        // Aura simply doesn't notify (the panel card still works).
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    // MARK: - proposals

    static func proposal(title: String, body: String, token: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.categoryIdentifier = proposalCategory
        content.userInfo = ["token": token]

        let request = UNNotificationRequest(
            identifier: proposalIdentifier(token),
            content: content,
            trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error {
                NSLog("Aura: notification failed: \(error.localizedDescription)")
            }
        }
    }

    static func proposalIdentifier(_ token: String) -> String {
        "aura.proposal.\(token)"
    }

    /// Drop any proposal notifications once the question is moot — the user
    /// answered in the panel, confirmed from the notification itself, or the
    /// engine timed the proposal out.
    static func clearProposals() {
        let center = UNUserNotificationCenter.current()
        center.getPendingNotificationRequests { requests in
            let ids = requests
                .filter { $0.content.categoryIdentifier == proposalCategory }
                .map { $0.identifier }
            center.removePendingNotificationRequests(withIdentifiers: ids)
        }
        center.getDeliveredNotifications { notifications in
            let ids = notifications
                .filter { $0.request.content.categoryIdentifier == proposalCategory }
                .map { $0.request.identifier }
            center.removeDeliveredNotifications(withIdentifiers: ids)
        }
    }
}

/// The notification delegate.
///
/// `willPresent` matters more than it looks: a menu-bar app is *always* the
/// active application while it runs, and without this callback macOS shows
/// no banner for notifications delivered to an active app — the exact
/// silence the notification exists to break.
final class Delegate: NSObject, UNUserNotificationCenterDelegate {

    static let shared = Delegate()

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler:
                                @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler:
                                @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let token = (info["token"] as? String) ?? ""
        let action = response.actionIdentifier
        Task { @MainActor in
            switch action {
            case Notify.runAction:
                Notify.onRun(token)
            case Notify.cancelAction:
                Notify.onCancel(token)
            default:
                Notify.onShow()   // tapped the body of the notification
            }
        }
        completionHandler()
    }
}
