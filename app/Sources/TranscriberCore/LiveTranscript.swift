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
        guard let path = dir.rawPath(), !path.isEmpty else { return .waiting }
        return read(rawAt: path)
    }

    public static func read(rawAt path: String) -> LiveTranscript {
        guard let raw = try? String(contentsOfFile: path, encoding: .utf8) else { return .waiting }
        let body = body(ofRaw: raw)
        return body.isEmpty ? .waiting : .text(body)
    }

    /// Everything after the heading line, without surrounding blank lines; no heading — nothing.
    public static func body(ofRaw raw: String) -> String {
        let lines = raw.components(separatedBy: "\n")
        guard let at = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespacesAndNewlines) == heading }) else {
            return ""
        }
        return lines[(at + 1)...].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// What to append when the new text only grew at the end — the usual case: the worker adds
    /// replies. Appending keeps the reader's selection and scroll; anything else (a reply re-split,
    /// a marker inserted) is a full replace.
    public static func appendix(from old: String, to new: String) -> String? {
        guard new.count > old.count, new.hasPrefix(old) else { return nil }
        return String(new.dropFirst(old.count))
    }
}

/// Polls one session's Raw cheaply: `state.json` holds every reply with its embedding indices and
/// grows with the meeting, so it is parsed only until `raw_path` shows up (the worker pins it for
/// the whole session), and the Raw is re-read only when its modification date or size moves.
public final class LiveTranscriptFollower {
    public let session: SessionDir
    private var rawPath: String?
    private var stamp: (Date, Int)?

    public init(session: SessionDir) {
        self.session = session
    }

    /// The current text when it may have changed since the last call; nil — nothing new.
    public func poll() -> LiveTranscript? {
        if rawPath == nil {
            rawPath = session.rawPath().flatMap { $0.isEmpty ? nil : $0 }
            guard rawPath != nil else { return stamp == nil ? markSeen(.waiting) : nil }
        }
        guard let rawPath,
              let attrs = try? FileManager.default.attributesOfItem(atPath: rawPath),
              let modified = attrs[.modificationDate] as? Date,
              let size = (attrs[.size] as? NSNumber)?.intValue else {
            return stamp == nil ? markSeen(.waiting) : nil
        }
        if let stamp, stamp.0 == modified, stamp.1 == size { return nil }
        stamp = (modified, size)
        return LiveTranscript.read(rawAt: rawPath)
    }

    /// The first poll always answers, so the window can show the placeholder straight away.
    private func markSeen(_ value: LiveTranscript) -> LiveTranscript {
        stamp = (.distantPast, -1)
        return value
    }
}
