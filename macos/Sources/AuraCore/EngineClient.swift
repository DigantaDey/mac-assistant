import Foundation

/// Everything the app needs to talk to its engine: an endpoint and the token.
public struct EngineEndpoint: Equatable, Sendable {
    public var host: String
    public var port: Int
    public var token: String?

    public init(host: String = "127.0.0.1", port: Int = 7331, token: String? = nil) {
        self.host = host
        self.port = port
        self.token = token
    }

    public var baseURL: URL {
        URL(string: "http://\(host):\(port)") ?? URL(string: "http://127.0.0.1:7331")!
    }
}

public enum EngineError: LocalizedError, Equatable {
    case unreachable(String)
    case unauthorized
    case refused(String)
    case http(Int, String)
    case malformed(String)

    public var errorDescription: String? {
        switch self {
        case .unreachable(let detail):
            return "Aura's engine isn't answering (\(detail))."
        case .unauthorized:
            return "Another Aura engine holds this port with a different key — restart Aura's engine."
        case .refused(let detail):
            return detail.isEmpty ? "Aura refused that request." : detail
        case .http(let code, let detail):
            return detail.isEmpty ? "Aura's engine answered \(code)." : detail
        case .malformed(let detail):
            return "Aura's engine sent something unexpected (\(detail))."
        }
    }

    public var isUnauthorized: Bool {
        if case .unauthorized = self { return true }
        return false
    }
}

/// A typed, async client for the engine's local API.
public final class EngineClient: @unchecked Sendable {
    public private(set) var endpoint: EngineEndpoint
    private let session: URLSession
    private let decoder = JSONDecoder()

    public init(endpoint: EngineEndpoint, timeout: TimeInterval = 5,
                sessionConfiguration: URLSessionConfiguration? = nil) {
        self.endpoint = endpoint
        let configuration = sessionConfiguration ?? URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        // The event stream is intentionally long-lived. A 60-second resource
        // cap silently reconnects it even though its request asks to live for
        // an hour, replaying stale UI state on every reconnect.
        configuration.timeoutIntervalForResource = 86_400
        configuration.httpMaximumConnectionsPerHost = 4
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        self.session = URLSession(configuration: configuration)
    }

    public func update(endpoint: EngineEndpoint) {
        self.endpoint = endpoint
    }

    // MARK: - reads

    public func health(timeout: TimeInterval = 2) async throws -> EngineHealth {
        try await get("/api/health", timeout: timeout)
    }

    public func state(timeout: TimeInterval? = nil) async throws -> EngineState {
        try await get("/api/state", timeout: timeout)
    }
    public func permissions() async throws -> PermissionsSnapshot { try await get("/api/permissions") }
    public func config() async throws -> EngineConfig { try await get("/api/config") }
    public func metrics() async throws -> EngineMetrics { try await get("/api/metrics") }
    public func training() async throws -> WakeTraining { try await get("/api/wake/train") }

    public func skills() async throws -> [SkillSpec] {
        let response: EngineSkills = try await get("/api/skills")
        return response.skills
    }

    public func history(limit: Int = 100) async throws -> [HistoryEntry] {
        let response: EngineHistory = try await get("/api/history")
        return Array(response.events.prefix(limit))
    }

    // MARK: - writes

    @discardableResult
    public func trigger() async throws -> EngineReply { try await post("/api/trigger") }

    @discardableResult
    public func send(text: String) async throws -> EngineReply {
        try await post("/api/input", body: ["text": text])
    }

    @discardableResult
    public func confirm(token: String) async throws -> EngineReply {
        try await post("/api/confirm", body: ["token": token])
    }

    @discardableResult
    public func cancel(token: String) async throws -> EngineReply {
        try await post("/api/cancel", body: ["token": token])
    }

    @discardableResult
    public func feedback(transcript: String, skill: String, good: Bool,
                         args: [String: Any]? = nil, note: String = "") async throws -> EngineReply {
        var body: [String: Any] = ["transcript": transcript, "skill": skill,
                                   "verdict": good ? "good" : "bad", "note": note]
        if let args, !args.isEmpty { body["args"] = args }
        return try await post("/api/correct", body: body)
    }

    @discardableResult
    public func updateConfig(section: String, key: String, value: JSONValue) async throws -> EngineReply {
        try await post("/api/config", body: ["updates": [section: [key: value]]])
    }

    @discardableResult
    public func setWakeMode(_ mode: String, phrase: String? = nil) async throws -> EngineReply {
        var body: [String: Any] = ["mode": mode]
        if let phrase { body["phrase"] = phrase }
        return try await post("/api/wake", body: body)
    }

    @discardableResult
    public func startTraining(phrase: String) async throws -> EngineReply {
        try await post("/api/wake/train", body: ["phrase": phrase])
    }

    @discardableResult
    public func captureSample() async throws -> EngineReply { try await post("/api/wake/train/capture") }
    @discardableResult
    public func finishTraining() async throws -> EngineReply {
        try await post("/api/wake/train/finish", timeout: 5)
    }
    @discardableResult
    public func cancelTraining() async throws -> EngineReply { try await post("/api/wake/train/cancel") }

    @discardableResult
    public func requestPermission(_ target: String) async throws -> EngineReply {
        try await post("/api/permissions/request", body: ["target": target], timeout: 5)
    }

    @discardableResult
    public func openSystemSettings(_ target: String) async throws -> EngineReply {
        try await post("/api/permissions/open", body: ["target": target])
    }

    @discardableResult
    public func testAutomation() async throws -> EngineReply {
        try await post("/api/permissions/test_automation")
    }

    @discardableResult
    public func startInstall() async throws -> EngineReply { try await post("/api/setup/install") }

    @discardableResult
    public func runSetupStep(_ step: String) async throws -> EngineReply {
        try await post("/api/setup/step", body: ["step": step], timeout: 600)
    }

    @discardableResult
    public func openFolder(_ what: String) async throws -> EngineReply {
        try await post("/api/system/open", body: ["what": what])
    }

    // MARK: - the event stream

    /// A long-lived, self-healing stream of engine events.
    ///
    /// The stream never ends on its own: if the engine restarts, the client
    /// reconnects with backoff and keeps yielding, so the UI has one code path
    /// for "everything that happens".
    public func events() -> AsyncStream<EngineEvent> {
        AsyncStream { continuation in
            let task = Task { [weak self] in
                var backoffSeconds: UInt64 = 1
                var lastEventID = 0
                var eventEpoch: String?
                while !Task.isCancelled {
                    guard let self else { break }
                    do {
                        var request = URLRequest(url: self.endpoint.baseURL.appendingPathComponent("api/events"))
                        request.timeoutInterval = 3600
                        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                        if lastEventID > 0 || eventEpoch != nil {
                            request.setValue(String(lastEventID), forHTTPHeaderField: "Last-Event-ID")
                        }
                        if let eventEpoch {
                            request.setValue(eventEpoch, forHTTPHeaderField: "X-Aura-Event-Epoch")
                        }
                        self.authorize(&request)

                        let (bytes, response) = try await self.session.bytes(for: request)
                        guard let http = response as? HTTPURLResponse else {
                            throw EngineError.malformed("no HTTP response on the event stream")
                        }
                        let newEpoch = http.value(forHTTPHeaderField: "X-Aura-Event-Epoch") ?? ""
                        if let eventEpoch, eventEpoch != newEpoch { lastEventID = 0 }
                        eventEpoch = newEpoch
                        guard http.statusCode == 200 else {
                            throw http.statusCode == 401 ? EngineError.unauthorized
                                : EngineError.http(http.statusCode, "event stream")
                        }
                        backoffSeconds = 1
                        var parser = SSEParser()
                        for try await line in bytes.lines {
                            guard let message = parser.feed(line: line) else { continue }
                            if let event = message.decode() {
                                // Replay only after this cursor and discard any
                                // duplicate that straddled a stream reconnect.
                                guard event.seq > lastEventID else { continue }
                                lastEventID = event.seq
                                continuation.yield(event)
                            }
                        }
                    } catch {
                        if Task.isCancelled { break }
                        try? await Task.sleep(nanoseconds: backoffSeconds * 1_000_000_000)
                        backoffSeconds = min(backoffSeconds * 2, 30)
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: - plumbing

    private func authorize(_ request: inout URLRequest) {
        if let token = endpoint.token, !token.isEmpty {
            request.setValue(token, forHTTPHeaderField: "X-Aura-Token")
        }
        request.setValue("application/json", forHTTPHeaderField: "Accept")
    }

    private func get<T: Decodable>(_ path: String, timeout: TimeInterval? = nil) async throws -> T {
        try await perform(path, method: "GET", body: nil, timeout: timeout)
    }

    private func post(_ path: String, body: [String: Any]? = nil,
                      timeout: TimeInterval? = nil) async throws -> EngineReply {
        try await perform(path, method: "POST", body: body ?? [:], timeout: timeout)
    }

    private func perform<T: Decodable>(_ path: String, method: String,
                                       body: [String: Any]?,
                                       timeout: TimeInterval?) async throws -> T {
        var request = URLRequest(url: endpoint.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        if let timeout { request.timeoutInterval = timeout }
        authorize(&request)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            if (error as? URLError)?.code == .cancelled { throw error }
            throw EngineError.unreachable((error as? URLError)?.code.name ?? error.localizedDescription)
        }

        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        switch status {
        case 200...299:
            do {
                return try decoder.decode(T.self, from: data)
            } catch {
                throw EngineError.malformed("couldn't read the reply")
            }
        case 401:
            throw EngineError.unauthorized
        case 403:
            throw EngineError.refused(Self.errorMessage(data) ?? "Aura refused that request.")
        default:
            throw EngineError.http(status, Self.errorMessage(data) ?? "")
        }
    }

    private static func errorMessage(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return (object["message"] as? String) ?? (object["error"] as? String)
    }
}

extension URLError.Code {
    var name: String {
        switch self {
        case .cannotConnectToHost, .cannotFindHost: return "nothing is listening"
        case .timedOut: return "it timed out"
        case .networkConnectionLost: return "the connection dropped"
        default: return "error \(rawValue)"
        }
    }
}
