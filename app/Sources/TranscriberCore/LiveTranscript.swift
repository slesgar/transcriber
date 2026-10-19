import Foundation

/// The text recognised so far, for the live transcript window. The source is the Raw file the
/// worker rewrites after every recognised block (`state.json` → `raw_path`): the window shows
/// exactly what will land in the meeting, and the rendering stays in one place — the worker.
public enum LiveTranscript: Equatable {
    /// No line yet: the worker has not started, has not written the Raw, or recognised nothing.
    case waiting
    case text(String)

    /// Heading the worker puts in front of the replies (`vault.render_raw`). Part of the file
    /// protocol between the two processes, so both sides spell it the same.
    public static let heading = "## Полный транскрипт"

    public static func read(_ dir: SessionDir) -> LiveTranscript {
        guard let path = dir.rawPath(), !path.isEmpty,
              let raw = try? String(contentsOfFile: path, encoding: .utf8) else { return .waiting }
        let body = body(ofRaw: raw)
        return body.isEmpty ? .waiting : .text(body)
    }

    /// Everything after the heading line, without surrounding blank lines; no heading — nothing.
    public static func body(ofRaw raw: String) -> String {
        let lines = raw.components(separatedBy: "\n")
        guard let at = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == heading }) else {
            return ""
        }
        return lines[(at + 1)...].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
