import XCTest
@testable import AuraCore

/// The event stream carries loosely-typed JSON; these tests pin how it is read.
final class JSONValueTests: XCTestCase {

    private func decode(_ json: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
    }

    func testScalars() throws {
        XCTAssertEqual(try decode("\"hello\"").stringValue, "hello")
        XCTAssertEqual(try decode("42").intValue, 42)
        XCTAssertEqual(try decode("4.5").doubleValue, 4.5)
        XCTAssertEqual(try decode("true").boolValue, true)
        XCTAssertEqual(try decode("false").boolValue, false)
        XCTAssertEqual(try decode("null"), .null)
    }

    func testObjectsAndArrays() throws {
        let value = try decode(#"{"state": "armed", "count": 3, "actions": [{"skill": "system.open_app"}]}"#)
        XCTAssertEqual(value["state"]?.stringValue, "armed")
        XCTAssertEqual(value["count"]?.intValue, 3)
        XCTAssertEqual(value["actions"]?.arrayValue?.count, 1)
        XCTAssertEqual(value["actions"]?.arrayValue?.first?["skill"]?.stringValue, "system.open_app")
        XCTAssertNil(value["missing"])
    }

    func testDisplayIsAlwaysReadable() throws {
        XCTAssertEqual(try decode("\"ready\"").display, "ready")
        XCTAssertEqual(try decode("7").display, "7")
        XCTAssertEqual(try decode("true").display, "yes")
        XCTAssertEqual(try decode("[1, 2]").display, "1, 2")
        XCTAssertFalse(try decode(#"{"b": 2, "a": 1}"#).display.isEmpty)
    }

    func testEventDecodingToleratesMissingFields() throws {
        // A future engine that drops or renames a key must not break the app.
        let event = try JSONDecoder().decode(EngineEvent.self,
                                             from: Data(#"{"type": "reply", "data": {"text": "Done."}}"#.utf8))
        XCTAssertEqual(event.type, "reply")
        XCTAssertEqual(event.text, "Done.")
        XCTAssertEqual(event.seq, 0)
        XCTAssertTrue(event.data["state"] == nil)
    }
}
