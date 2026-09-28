import Foundation

/// The app's log: one file, rotation at 5 MB, readable from inside the app.
///
/// Both the Swift shell and the Python engine write here, so "show me what
/// happened" is a single, honest answer — and it never grows without bound.
public final class AuraLog: @unchecked Sendable {

    public static let shared = AuraLog()

    public let url: URL
    private let limit: Int
    private let lock = NSLock()
    private var writer: FileHandle?

    public init(url: URL = AppPaths.logURL, limitBytes: Int = 5_000_000) {
        self.url = url
        self.limit = limitBytes
        prepare()
    }

    // MARK: - writing

    public func write(_ message: String) {
        let stamp = AuraLog.formatter.string(from: Date())
        append("[\(stamp)] \(message)\n")
    }

    /// A file handle for a child process's stdout/stderr (the engine).
    public func childHandle() -> FileHandle? {
        lock.lock()
        defer { lock.unlock() }
        rotateIfNeeded()
        return writer
    }

    private func append(_ text: String) {
        lock.lock()
        defer { lock.unlock() }
        rotateIfNeeded()
        guard let writer, let data = text.data(using: .utf8) else { return }
        try? writer.write(contentsOf: data)
    }

    private func prepare() {
        let directory = url.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        writer = try? FileHandle(forWritingTo: url)
        _ = try? writer?.seekToEnd()
    }

    private func rotateIfNeeded() {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = attributes[.size] as? NSNumber,
              size.intValue > limit
        else { return }
        try? writer?.close()
        writer = nil
        let previous = url.appendingPathExtension("1")
        try? FileManager.default.removeItem(at: previous)
        try? FileManager.default.moveItem(at: url, to: previous)
        FileManager.default.createFile(atPath: url.path, contents: nil)
        writer = try? FileHandle(forWritingTo: url)
    }

    // MARK: - reading (the in-app log viewer)

    /// The last `count` lines, without loading a 5 MB file into memory.
    public func tail(lines count: Int = 200, byteCap: Int = 96_000) -> [String] {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return [] }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        let start = size > UInt64(byteCap) ? size - UInt64(byteCap) : 0
        try? handle.seek(toOffset: start)
        guard let data = try? handle.readToEnd(), let text = String(data: data, encoding: .utf8) else {
            return []
        }
        let all = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        return Array(all.suffix(count))
    }

    private static let formatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()
}
