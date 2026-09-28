import Foundation

/// The engine's `/api/events` stream, as the app sees it.
public struct EngineEvent: Decodable, Identifiable, Sendable {
    public let seq: Int
    public let ts: Double
    public let type: String
    public let data: [String: JSONValue]

    public var id: Int { seq }

    public var text: String? { data["text"]?.stringValue }
    public var state: String? { data["state"]?.stringValue }
    public var session: String? { data["session"]?.stringValue }
    public var line: String? { data["line"]?.stringValue }
    public var token: String? { data["token"]?.stringValue }
    public var skill: String? { data["skill"]?.stringValue }
    public var message: String? { data["message"]?.stringValue }
    public var reason: String? { data["reason"]?.stringValue }

    public init(seq: Int, ts: Double, type: String, data: [String: JSONValue]) {
        self.seq = seq
        self.ts = ts
        self.type = type
        self.data = data
    }

    private enum CodingKeys: String, CodingKey { case seq, ts, type, data }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        seq = (try? container.decode(Int.self, forKey: .seq)) ?? 0
        ts = (try? container.decode(Double.self, forKey: .ts)) ?? Date().timeIntervalSince1970
        type = (try? container.decode(String.self, forKey: .type)) ?? "unknown"
        data = (try? container.decode([String: JSONValue].self, forKey: .data)) ?? [:]
    }
}

/// One `event:`/`data:` frame from an SSE stream.
public struct SSEMessage: Equatable {
    public var event: String?
    public var data: String
    public var id: String?

    public init(event: String? = nil, data: String = "", id: String? = nil) {
        self.event = event
        self.data = data
        self.id = id
    }

    public func decode() -> EngineEvent? {
        guard let payload = data.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(EngineEvent.self, from: payload)
    }
}

/// A line-oriented SSE parser — the smallest correct thing that works.
///
/// Handles `data:` continuations, `id:`, `event:`, `retry:`, and `:comment`
/// keepalives (which must NOT be mistaken for a message boundary).
public struct SSEParser {
    private var event: String?
    private var id: String?
    private var dataLines: [String] = []

    public init() {}

    /// Feed one line (without its newline). Returns a message when one completes.
    public mutating func feed(line: String) -> SSEMessage? {
        if line.isEmpty {
            guard !dataLines.isEmpty || event != nil else { return nil }
            let message = SSEMessage(event: event, data: dataLines.joined(separator: "\n"), id: id)
            event = nil
            id = nil
            dataLines.removeAll()
            return message
        }
        if line.hasPrefix(":") { return nil }          // keepalive / comment

        let (field, value) = Self.split(line)
        switch field {
        case "event": event = value
        case "data": dataLines.append(value)
        case "id": id = value
        case "retry": break                            // the client owns backoff
        default: break
        }
        return nil
    }

    /// Feed a whole SSE payload (used by tests and by `bytes` consumers).
    public mutating func feed(block: String) -> [SSEMessage] {
        var messages: [SSEMessage] = []
        for line in block.split(separator: "\n", omittingEmptySubsequences: false) {
            if let message = feed(line: String(line)) { messages.append(message) }
        }
        return messages
    }

    private static func split(_ line: String) -> (String, String) {
        guard let index = line.firstIndex(of: ":") else { return (line, "") }
        let field = String(line[line.startIndex..<index])
        var value = String(line[line.index(after: index)...])
        if value.hasPrefix(" ") { value.removeFirst() }
        return (field, value)
    }
}
