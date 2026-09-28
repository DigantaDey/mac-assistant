import XCTest
@testable import AuraCore

/// The event stream is the app's nervous system — a parser bug shows up as a
/// UI that silently stops updating, so it gets its own tests.
final class SSEParserTests: XCTestCase {

    func testASingleFrame() {
        var parser = SSEParser()
        XCTAssertNil(parser.feed(line: "id: 7"))
        XCTAssertNil(parser.feed(line: "event: reply"))
        XCTAssertNil(parser.feed(line: #"data: {"seq": 7, "ts": 1.0, "type": "reply", "data": {"text": "Done."}}"#))
        let message = parser.feed(line: "")
        XCTAssertEqual(message?.event, "reply")
        XCTAssertEqual(message?.id, "7")
        let event = message?.decode()
        XCTAssertEqual(event?.type, "reply")
        XCTAssertEqual(event?.text, "Done.")
    }

    func testMultipleDataLinesAreJoined() {
        var parser = SSEParser()
        _ = parser.feed(line: "data: {\"a\": 1,")
        _ = parser.feed(line: "data: \"b\": 2}")
        let message = parser.feed(line: "")
        XCTAssertEqual(message?.data, "{\"a\": 1,\n\"b\": 2}")
    }

    func testAWholeBlockCanBeFedAtOnce() {
        var parser = SSEParser()
        let block = "retry: 2000\n\nevent: state\ndata: {\"type\": \"state\", \"seq\": 1, \"ts\": 1, \"data\": {\"state\": \"armed\"}}\n\n"
        let messages = parser.feed(block: block)
        XCTAssertEqual(messages.count, 1)
        XCTAssertEqual(messages.first?.decode()?.state, "armed")
    }

    func testKeepaliveCommentsAreNotMessages() {
        var parser = SSEParser()
        XCTAssertNil(parser.feed(line: ": keepalive"))
        XCTAssertNil(parser.feed(line: ""))
        XCTAssertTrue(parser.feed(block: ": keepalive\n\n").isEmpty)
    }

    func testFieldWithoutSpaceIsAccepted() {
        var parser = SSEParser()
        _ = parser.feed(line: "event:config")
        _ = parser.feed(line: "data:{\"changed\": []}")
        let message = parser.feed(line: "")
        XCTAssertEqual(message?.event, "config")
    }
}
