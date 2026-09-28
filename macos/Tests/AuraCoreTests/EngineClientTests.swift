import XCTest
@testable import AuraCore

/// A stub transport, so the client's real behaviour (headers, status mapping,
/// decoding) is tested without a running engine.
final class StubURLProtocol: URLProtocol {
    static var respond: ((URLRequest) -> (Int, Data))?
    static var fail: Error?
    static var lastRequest: URLRequest?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        StubURLProtocol.lastRequest = request
        if let fail = StubURLProtocol.fail {
            client?.urlProtocol(self, didFailWithError: fail)
            return
        }
        guard let respond = StubURLProtocol.respond else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let (status, data) = respond(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                       httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private func makeClient(token: String? = "secret-token") -> EngineClient {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubURLProtocol.self]
    return EngineClient(endpoint: EngineEndpoint(port: 7331, token: token),
                        timeout: 3,
                        sessionConfiguration: configuration)
}

final class EngineClientTests: XCTestCase {

    override func setUp() {
        super.setUp()
        StubURLProtocol.respond = nil
        StubURLProtocol.fail = nil
        StubURLProtocol.lastRequest = nil
    }

    override func tearDown() {
        StubURLProtocol.respond = nil
        StubURLProtocol.fail = nil
        StubURLProtocol.lastRequest = nil
        super.tearDown()
    }

    func testEveryRequestCarriesTheToken() async throws {
        StubURLProtocol.respond = { _ in (200, Data(#"{"ok": true, "state": "armed"}"#.utf8)) }
        let client = makeClient()
        _ = try await client.health()
        XCTAssertEqual(StubURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-Aura-Token"),
                       "secret-token")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.absoluteString,
                       "http://127.0.0.1:7331/api/health")
    }

    func testAForeignEngineIsReportedAsUnauthorized() async {
        StubURLProtocol.respond = { _ in (401, Data(#"{"ok": false, "error": "no"}"#.utf8)) }
        let client = makeClient()
        do {
            _ = try await client.state()
            XCTFail("a 401 must not be treated as success")
        } catch let error as EngineError {
            XCTAssertTrue(error.isUnauthorized)
        } catch {
            XCTFail("unexpected error: \(error)")
        }
    }

    func testASilentEngineIsReportedAsUnreachable() async {
        StubURLProtocol.fail = URLError(.cannotConnectToHost)
        let client = makeClient()
        do {
            _ = try await client.health()
            XCTFail("a connection failure must throw")
        } catch let error as EngineError {
            if case .unreachable = error { return }
            XCTFail("expected .unreachable, got \(error)")
        } catch {
            XCTFail("unexpected error: \(error)")
        }
    }

    func testARefusalSurfacesTheEnginesOwnMessage() async {
        StubURLProtocol.respond = { _ in
            (403, Data(#"{"ok": false, "message": "Aura's engine only answers the Aura app."}"#.utf8))
        }
        let client = makeClient()
        do {
            _ = try await client.permissions()
            XCTFail("403 must be refused")
        } catch let error as EngineError {
            XCTAssertEqual(error.errorDescription, "Aura's engine only answers the Aura app.")
        } catch {
            XCTFail("unexpected error: \(error)")
        }
    }

    func testStateAndConfigDecode() async throws {
        let state = #"{"state":"proposing","version":"0.6.0","wake_mode":"openwakeword","mic_ready":true,"planner_online":false,"planner":{"engine":"openai_compat","model":"qwen3:4b","base_url":"http://127.0.0.1:11434/v1","last_error":"connection refused"},"session":{"id":"abc","transcript":"empty the trash","token":"deadbeef"}}"#
        let config = #"{"version":"0.6.0","resolved_profile":"mac","data_dir":"/tmp","port":7331,"live":{"tts":{"enabled":true,"rate":178},"safety":{"show_plan_before_run":false}},"planner":{"model":"qwen3:4b"},"files":{"user_config":"/tmp/config.toml","runtime":"/tmp/runtime.toml"}}"#

        StubURLProtocol.respond = { request in
            request.url?.path == "/api/state" ? (200, Data(state.utf8)) : (200, Data(config.utf8))
        }
        let client = makeClient()
        let snapshot = try await client.state()
        XCTAssertEqual(snapshot.state, "proposing")
        XCTAssertEqual(snapshot.session?.token, "deadbeef")
        XCTAssertEqual(snapshot.planner?.lastError, "connection refused")
        XCTAssertEqual(snapshot.micReady, true)

        let settings = try await client.config()
        XCTAssertEqual(settings.resolvedProfile, "mac")
        XCTAssertEqual(settings.liveValue("tts", "rate")?.intValue, 178)
        XCTAssertEqual(settings.liveValue("safety", "show_plan_before_run")?.boolValue, false)
    }

    func testWritesArePostsWithJSONBodies() async throws {
        StubURLProtocol.respond = { _ in (200, Data(#"{"ok": true, "accepted": true}"#.utf8)) }
        let client = makeClient()
        let reply = try await client.send(text: "open spotify")
        XCTAssertTrue(reply.accepted ?? false)
        XCTAssertEqual(StubURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/api/input")

        var bodyText: String?
        if let stream = StubURLProtocol.lastRequest?.httpBodyStream {
            stream.open()
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 512)
            while stream.hasBytesAvailable {
                let read = stream.read(&buffer, maxLength: buffer.count)
                if read <= 0 { break }
                data.append(buffer, count: read)
            }
            stream.close()
            bodyText = String(data: data, encoding: .utf8)
        }
        XCTAssertEqual(bodyText, #"{"text":"open spotify"}"#)
    }

    func testEndpointsStayOnLoopback() {
        XCTAssertEqual(EngineEndpoint(port: 9999, token: nil).baseURL.absoluteString,
                       "http://127.0.0.1:9999")
        XCTAssertEqual(EngineEndpoint().port, 7331)
    }
}
