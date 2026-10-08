import Foundation

// Typed views of the engine's JSON. Every field is optional-tolerant: a newer
// engine must never crash an older app, and vice versa.

public struct EngineHealth: Decodable, Sendable {
    public let ok: Bool
    public let state: String
    public let bridge: String?
    public let micReady: Bool?
    public let plannerOnline: Bool?
    public let auth: Bool?
    public let version: String?

    private enum CodingKeys: String, CodingKey {
        case ok, state, bridge, auth, version
        case micReady = "mic_ready"
        case plannerOnline = "planner_online"
    }
}

public struct EngineState: Decodable, Sendable {
    public let state: String
    public let version: String?
    public let profile: String?
    public let bridge: String?
    public let wakeMode: String?
    public let wakePhrase: String?
    public let wakeEngine: String?
    public let wakeActive: Bool?
    public let wakeError: String?
    public let wakeDetail: String?
    public let ttsEnabled: Bool?
    public let askBeforeRun: Bool?
    public let dataDir: String?
    public let micReady: Bool?
    public let plannerOnline: Bool?
    public let auth: Bool?
    public let planner: Planner?
    public let session: Session?

    public struct Planner: Decodable, Sendable {
        public let engine: String?
        public let model: String?
        public let baseURL: String?
        public let lastError: String?

        private enum CodingKeys: String, CodingKey {
            case engine, model
            case baseURL = "base_url"
            case lastError = "last_error"
        }
    }

    public struct Session: Decodable, Sendable {
        public let id: String?
        public let transcript: String?
        public let token: String?
    }

    private enum CodingKeys: String, CodingKey {
        case state, version, profile, bridge, planner, session, auth
        case wakeMode = "wake_mode"
        case wakePhrase = "wake_phrase"
        case wakeEngine = "wake_engine"
        case wakeActive = "wake_active"
        case wakeError = "wake_error"
        case wakeDetail = "wake_detail"
        case ttsEnabled = "tts_enabled"
        case askBeforeRun = "ask_before_run"
        case dataDir = "data_dir"
        case micReady = "mic_ready"
        case plannerOnline = "planner_online"
    }
}

public struct PermissionsSnapshot: Decodable, Sendable {
    public let platform: String?
    public let profile: String?
    public let resolvedProfile: String?
    public let bridge: String?
    public let microphone: Bool?
    public let accessibility: Bool?
    /// The engine's own words about the Accessibility state. It already names
    /// the app macOS filed the grant under, which is the part that turns "not
    /// granted" into an instruction the user can follow.
    public let accessibilityDetail: String?
    public let whisperCpp: Bool?
    public let plannerServer: Bool?
    public let plannerEngine: String?
    public let model: String?
    public let wakeModels: WakeModels?

    public struct WakeModels: Decodable, Sendable {
        public let ready: Bool
        public let detail: String
    }

    private enum CodingKeys: String, CodingKey {
        case platform, profile, bridge, microphone, accessibility, model
        case resolvedProfile = "resolved_profile"
        case accessibilityDetail = "accessibility_detail"
        case whisperCpp = "whisper_cpp"
        case plannerServer = "planner_server"
        case plannerEngine = "planner_engine"
        case wakeModels = "wake_models"
    }

    /// True when every *permission* (not component) is in place.
    public var isMac: Bool { (platform ?? "other") == "mac" }
}

public struct SkillSpec: Decodable, Identifiable, Sendable {
    public let name: String
    public let description: String
    public let risk: String?
    public let examples: [String]?

    public var id: String { name }

    /// "system.open_app" → "Open app"
    public var title: String {
        let tail = name.split(separator: ".").last.map(String.init) ?? name
        return tail.replacingOccurrences(of: "_", with: " ").capitalized
    }

    public var group: String {
        name.split(separator: ".").first.map { String($0).capitalized } ?? "Other"
    }
}

public struct EngineSkills: Decodable, Sendable {
    public let skills: [SkillSpec]
}

public struct HistoryEntry: Decodable, Identifiable, Sendable {
    public let id: Int
    public let ts: Double
    public let transcript: String
    public let reply: String
    public let outcome: String
    public let totalMs: Int
    public let plan: Plan?

    public struct Plan: Decodable, Sendable {
        public let reply: String?
        public let actions: [PlanAction]?

        public struct PlanAction: Decodable, Sendable {
            public let skill: String?
            public let args: [String: JSONValue]?
        }
    }

    public var date: Date { Date(timeIntervalSince1970: ts) }

    /// The skill a 👍/👎 verdict supervises. Without it, feedback can't be
    /// attributed to anything the model could learn from.
    public var primarySkill: String? {
        plan?.actions?.lazy.compactMap { $0.skill }.first { !$0.isEmpty }
    }

    /// The action's arguments, as plain Foundation values for a request body.
    public var primaryArgs: [String: Any]? {
        guard let args = plan?.actions?
            .first(where: { ($0.skill ?? "").isEmpty == false })?.args else { return nil }
        return args.mapValues { $0.anyValue }
    }

    private enum CodingKeys: String, CodingKey {
        case id, ts, transcript, reply, outcome, plan
        case totalMs = "total_ms"
    }
}

public struct EngineHistory: Decodable, Sendable {
    public let events: [HistoryEntry]
}

public struct EngineConfig: Decodable, Sendable {
    public let version: String?
    public let profile: String?
    public let resolvedProfile: String?
    public let dataDir: String?
    public let host: String?
    public let port: Int?
    public let live: [String: [String: JSONValue]]
    public let planner: Planner?
    public let stt: STT?
    public let wakeModels: [String]?
    public let files: Files?

    public struct Planner: Decodable, Sendable {
        public let engine: String?
        public let model: String?
        public let baseURL: String?
        private enum CodingKeys: String, CodingKey {
            case engine, model
            case baseURL = "base_url"
        }
    }

    public struct STT: Decodable, Sendable {
        public let engine: String?
        public let language: String?
    }

    public struct Files: Decodable, Sendable {
        public let userConfig: String?
        public let runtime: String?
        private enum CodingKeys: String, CodingKey {
            case userConfig = "user_config"
            case runtime
        }
    }

    private enum CodingKeys: String, CodingKey {
        case version, profile, host, port, live, planner, stt, files
        case resolvedProfile = "resolved_profile"
        case dataDir = "data_dir"
        case wakeModels = "wake_models"
    }

    public func liveValue(_ section: String, _ key: String) -> JSONValue? {
        live[section]?[key]
    }
}

public struct WakeTraining: Decodable, Sendable {
    public let active: Bool
    public let phrase: String?
    public let count: Int?
    public let need: Int?
    public let listening: Bool?
    public let message: String?

    public init(active: Bool, phrase: String?, count: Int?, need: Int?, listening: Bool?,
                message: String? = nil) {
        self.active = active
        self.phrase = phrase
        self.count = count
        self.need = need
        self.listening = listening
        self.message = message
    }

    public var progress: Double {
        guard let need, need > 0, let count else { return 0 }
        return min(1, Double(count) / Double(need))
    }
}

public struct EngineMetrics: Decodable, Sendable {
    public let rssMB: Double?
    public let state: String?
    public let wakeMode: String?
    public let wakeEngine: String?
    public let wakeActive: Bool?
    public let wakeError: String?
    public let wakeDetail: String?
    public let sttEngine: String?
    public let examples: Examples?

    public struct Examples: Decodable, Sendable {
        public let total: Int?
        public let corrected: Int?
        public let cancelled: Int?
        public let confirmed: Int?
    }

    private enum CodingKeys: String, CodingKey {
        case state, examples
        case rssMB = "rss_mb"
        case wakeMode = "wake_mode"
        case wakeEngine = "wake_engine"
        case wakeActive = "wake_active"
        case wakeError = "wake_error"
        case wakeDetail = "wake_detail"
        case sttEngine = "stt_engine"
    }
}

/// The engine's answer to any "please do this" call: `{ok, message}`.
public struct EngineReply: Decodable, Sendable {
    public let ok: Bool
    public let message: String?
    public let status: String?
    public let detail: String?
    public let key: String?
    public let started: Bool?
    public let accepted: Bool?
    public let mode: String?
    public let phrase: String?
    public let engine: String?
    public let need: Int?
    public let minimum: Int?

    public var errorText: String? { ok ? nil : (message ?? "Aura couldn't do that.") }
}
