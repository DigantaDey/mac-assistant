import XCTest
@testable import AuraCore

/// The Activity timeline feeds 👍/👎 verdicts back to the decision model's
/// fine-tune. That only works when the history entries still carry the plan
/// — these tests pin the decoding that makes `primarySkill`/`primaryArgs`
/// possible.
final class HistoryEntryTests: XCTestCase {

    func testPlanSkillSurviveDecoding() throws {
        let json = """
        {"events": [{"id": 1, "ts": 1700000000, "transcript": "open youtube.com in safari",
          "reply": "Opened.", "outcome": "ok", "total_ms": 120,
          "plan": {"reply": "Opening YouTube.",
                   "actions": [{"skill": "browser.open_url",
                                "args": {"url": "youtube.com", "browser": "Safari"}}]}}]}
        """
        let decoded = try JSONDecoder().decode(EngineHistory.self, from: Data(json.utf8))
        let entry = try XCTUnwrap(decoded.events.first)
        XCTAssertEqual(entry.primarySkill, "browser.open_url")
        let args = try XCTUnwrap(entry.primaryArgs)
        XCTAssertEqual(args["url"] as? String, "youtube.com")
        XCTAssertEqual(args["browser"] as? String, "Safari")
    }

    func testHistoryWithoutAPlanYieldsNoSkill() throws {
        let json = """
        {"events": [{"id": 2, "ts": 1700000000, "transcript": "hello",
          "reply": "hi", "outcome": "ok", "total_ms": 5}]}
        """
        let decoded = try JSONDecoder().decode(EngineHistory.self, from: Data(json.utf8))
        let entry = try XCTUnwrap(decoded.events.first)
        XCTAssertNil(entry.primarySkill)
        XCTAssertNil(entry.primaryArgs)
    }

    func testUnknownExtraFieldsNeverBreakDecoding() throws {
        let json = """
        {"events": [{"id": 3, "ts": 1700000000, "transcript": "open safari",
          "reply": "Opened.", "outcome": "ok", "total_ms": 5,
          "plan": {"reply": "", "actions": [], "new_field": {"x": 1}}}]}
        """
        let decoded = try JSONDecoder().decode(EngineHistory.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.events.count, 1)
        XCTAssertNil(decoded.events.first?.primarySkill)
    }
}

final class WakeTrainingModelTests: XCTestCase {
    func testTrainingStatusCarriesVisibleCaptureMessage() throws {
        let json = #"{"active":true,"phrase":"hey aura","count":2,"need":6,"listening":true,"message":"Recording — say it now."}"#
        let status = try JSONDecoder().decode(WakeTraining.self, from: Data(json.utf8))
        XCTAssertTrue(status.active)
        XCTAssertEqual(status.count, 2)
        XCTAssertTrue(status.listening ?? false)
        XCTAssertEqual(status.message, "Recording — say it now.")
    }

    func testOlderTrainingStatusMayOmitMessage() throws {
        let json = #"{"active":true,"phrase":"hey aura","count":0,"need":6,"listening":false}"#
        let status = try JSONDecoder().decode(WakeTraining.self, from: Data(json.utf8))
        XCTAssertNil(status.message)
    }
}

/// The Wake Phrase panel's listening meter and retrain notice ride on the
/// state snapshot: a miscalibrated template must be *visible*, never silent.
final class EngineStateWakeTelemetryTests: XCTestCase {

    func testDecodesLiveListeningTelemetry() throws {
        let json = """
        {"state":"armed","version":"0.7.2","wake_mode":"openwakeword",
         "wake_engine":"TemplateWakeEngine","wake_active":true,
         "wake_detail":"Always listening is active — say “hey aura”.",
         "wake_notice":"","wake_level":0.9812,"wake_threshold":0.9753,"wake_fires":2}
        """
        let state = try JSONDecoder().decode(EngineState.self, from: Data(json.utf8))
        XCTAssertEqual(state.wakeActive, true)
        XCTAssertEqual(state.wakeLevel, 0.9812)
        XCTAssertEqual(state.wakeThreshold, 0.9753)
        XCTAssertEqual(state.wakeFires, 2)
        XCTAssertEqual(state.wakeNotice, "")
    }

    func testNullTelemetryDecodesToNil() throws {
        // Python None lands as JSON null: nothing scored recently must not
        // crash the decoder or invent a level.
        let json = """
        {"state":"armed","wake_active":true,"wake_notice":"",
         "wake_level":null,"wake_threshold":0.9753,"wake_fires":null}
        """
        let state = try JSONDecoder().decode(EngineState.self, from: Data(json.utf8))
        XCTAssertNil(state.wakeLevel)
        XCTAssertEqual(state.wakeThreshold, 0.9753)
        XCTAssertNil(state.wakeFires)
    }

    func testOlderEngineWithoutTelemetryStillDecodes() throws {
        let json = """
        {"state":"armed","version":"0.7.1","wake_mode":"manual","wake_active":false}
        """
        let state = try JSONDecoder().decode(EngineState.self, from: Data(json.utf8))
        XCTAssertNil(state.wakeLevel)
        XCTAssertNil(state.wakeThreshold)
        XCTAssertNil(state.wakeNotice)
    }

    func testLegacyTemplateNoticeSurvivesDecoding() throws {
        let json = """
        {"state":"armed","wake_active":true,
         "wake_notice":"This phrase was trained by an older Aura build and may be hard to trigger — train it again under Settings ▸ Wake Phrase."}
        """
        let state = try JSONDecoder().decode(EngineState.self, from: Data(json.utf8))
        XCTAssertTrue(state.wakeNotice?.contains("train it again") ?? false)
    }
}

final class JSONValueAnyTests: XCTestCase {

    func testAnyValueRoundTripsPrimitives() throws {
        let json = """
        {"url": "youtube.com", "level": 30, "ratio": 1.5, "on": true, "tags": ["a", "b"]}
        """
        let decoded = try JSONDecoder().decode([String: JSONValue].self, from: Data(json.utf8))
        XCTAssertEqual(decoded["url"]?.anyValue as? String, "youtube.com")
        XCTAssertEqual(decoded["level"]?.anyValue as? Int, 30)
        XCTAssertEqual(decoded["ratio"]?.anyValue as? Double, 1.5)
        XCTAssertEqual(decoded["on"]?.anyValue as? Bool, true)
        XCTAssertEqual((decoded["tags"]?.anyValue as? [Any])?.count, 2)
        XCTAssertTrue(JSONSerialization.isValidJSONObject(decoded.mapValues { $0.anyValue }))
    }
}
