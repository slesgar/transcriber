import Foundation

public final class Log {
    public static let shared = Log(file: FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/transcriber-app.log"))

    private let file: URL
    private let queue = DispatchQueue(label: "transcriber.log")
    private let formatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f
    }()

    public init(file: URL) {
        self.file = file
        try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
    }

    public func info(_ message: String) { write("INFO", message) }
    public func warn(_ message: String) { write("WARN", message) }
    public func error(_ message: String) { write("ERROR", message) }

    private func write(_ level: String, _ message: String) {
        let line = "\(formatter.string(from: Date())) \(level) \(message)\n"
        queue.sync {
            if let handle = try? FileHandle(forWritingTo: file) {
                handle.seekToEndOfFile()
                handle.write(line.data(using: .utf8)!)
                try? handle.close()
            } else {
                try? line.write(to: file, atomically: true, encoding: .utf8)
            }
        }
    }
}
