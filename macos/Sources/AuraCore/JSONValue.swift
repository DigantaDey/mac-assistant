import Foundation

/// A JSON value whose shape we don't know at compile time.
///
/// The engine's SSE stream carries a dozen different event payloads; typing
/// each one would mean a brittle model per event and a decode failure every
/// time the engine grows a field. `JSONValue` keeps the stream resilient —
/// unknown keys are simply ignored by the views that don't care.
public enum JSONValue: Decodable, Equatable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container,
                                                  debugDescription: "Unsupported JSON value")
        }
    }

    // MARK: accessors

    public var stringValue: String? {
        switch self {
        case .string(let value): return value
        case .number(let value): return value == value.rounded() ? String(Int(value)) : String(value)
        case .bool(let value): return value ? "true" : "false"
        default: return nil
        }
    }

    public var doubleValue: Double? {
        switch self {
        case .number(let value): return value
        case .string(let value): return Double(value)
        case .bool(let value): return value ? 1 : 0
        default: return nil
        }
    }

    public var intValue: Int? {
        guard let value = doubleValue else { return nil }
        return Int(value)
    }

    public var boolValue: Bool? {
        switch self {
        case .bool(let value): return value
        case .number(let value): return value != 0
        case .string(let value): return ["true", "yes", "1"].contains(value.lowercased())
        default: return nil
        }
    }

    public var arrayValue: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }

    public var objectValue: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }

    public subscript(key: String) -> JSONValue? {
        objectValue?[key]
    }

    /// A compact, human-readable rendering for UI that must show *something*.
    public var display: String {
        switch self {
        case .string(let value): return value
        case .number(let value): return value == value.rounded() ? String(Int(value)) : String(value)
        case .bool(let value): return value ? "yes" : "no"
        case .null: return "—"
        case .array(let values): return values.map(\.display).joined(separator: ", ")
        case .object(let values):
            return values.keys.sorted()
                .map { "\($0): \(values[$0]?.display ?? "")" }
                .joined(separator: ", ")
        }
    }

    /// Plain Foundation values, for building request bodies back to the
    /// engine (e.g. the action args attached to a 👍/👎 verdict).
    public var anyValue: Any {
        switch self {
        case .string(let value): return value
        case .number(let value):
            return value == value.rounded() ? Int(value) : value
        case .bool(let value): return value
        case .array(let values): return values.map { $0.anyValue }
        case .object(let values): return values.mapValues { $0.anyValue }
        case .null: return NSNull()
        }
    }
}
