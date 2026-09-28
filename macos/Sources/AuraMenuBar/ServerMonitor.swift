import Foundation

// Polls /api/health every 2 s and maps the orchestrator's state to a
// menu-bar icon state. Polling (not SSE) is deliberate: one tiny request,
// no reconnect logic, imperceptible load.

final class ServerMonitor {

    enum State { case armed, busy, attention, down }

    private(set) var state: State = .down
    private var timer: Timer?
    private let onChange: (State) -> Void

    init(onChange: @escaping (State) -> Void) {
        self.onChange = onChange
    }

    func start() {
        timer?.invalidate()
        let poller = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.poll()
        }
        timer = poller
        poll()
    }

    /// Re-check immediately (e.g. the Python process just started or died).
    func kick() {
        poll()
    }

    private func poll() {
        let port = UserDefaults.standard.object(forKey: "port") as? Int ?? 7331
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self = self else { return }
            var next = State.down
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let data = data,
               let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                switch payload["state"] as? String {
                case "proposing":
                    next = .attention
                case "capturing", "transcribing", "planning", "executing", "responding":
                    next = .busy
                default:
                    next = .armed
                }
            }
            DispatchQueue.main.async { self.set(next) }
        }.resume()
    }

    private func set(_ next: State) {
        guard next != state else { return }
        state = next
        onChange(next)
    }
}
