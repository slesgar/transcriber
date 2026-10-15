import ApplicationServices
import Foundation

/// One tile of the Толк grid, as the Accessibility tree shows it.
public struct TalkParticipant: Equatable {
    public var name: String
    /// `false` when the tile carries the `mute` icon class — the only mic state Толк publishes.
    public var micOn: Bool
    /// Служебный флаг разбора: метка `active-speaker` без `dominant`. После `parse` всегда false.
    var recentOnly = false
    /// The tile Толк marks as the one currently speaking (`active-speaker`).
    public var active: Bool

    public init(name: String, micOn: Bool, active: Bool) {
        self.name = name
        self.micOn = micOn
        self.active = active
    }
}

/// Who is in the call and who is talking, read off one AX dump.
///
/// The shape it parses was established by the reconnaissance of 23.09.2026 (a live Толк call seen
/// through Chrome): inside `AXGroup class='participants'` every tile is `AXGroup
/// class='grid-participant …'` holding an `AXButton class='participant-info-content …'` whose
/// title is the name, an `AXButton class='participant-icon …'` whose class list carries `mute`,
/// and — for the speaker — a descendant whose class list contains `active-speaker`.
///
/// Tiles with no name are dropped on purpose: the pagination tile and the owner's own preview are
/// both nameless, and neither is a participant.
public struct TalkSnapshot: Equatable {
    public var participants: [TalkParticipant]

    public init(participants: [TalkParticipant]) {
        self.participants = participants
    }

    /// The one tile Толк marks as speaking, if any.
    public var activeName: String? { participants.first(where: { $0.active })?.name }

    private static let titlePattern = try! NSRegularExpression(pattern: #"title='(.*?)'"#)
    private static let valuePattern = try! NSRegularExpression(pattern: #"value='(.*?)'"#)
    private static let classPattern = try! NSRegularExpression(pattern: #"class='(.*?)'"#)

    /// nil when the dump has no `participants` group at all — not in a call, or the web content of
    /// the tab is not exposed (wrong tab in front, Chrome not yet warmed up). That is a different
    /// answer from «a call with nobody in it», and the observer treats it as such.
    public static func parse(_ dump: String) -> TalkSnapshot? {
        let rows = self.rows(of: dump)
        guard let start = rows.firstIndex(where: { classes(of: $0.text).contains("participants") }) else {
            return nil
        }
        let base = rows[start].indent
        var end = start + 1
        while end < rows.count, rows[end].indent > base { end += 1 }
        let subtree = rows[(start + 1)..<end]

        var out: [TalkParticipant] = []
        var i = subtree.startIndex
        while i < subtree.endIndex {
            guard classes(of: subtree[i].text).contains("grid-participant") else {
                i += 1
                continue
            }
            let tileIndent = subtree[i].indent
            var j = i + 1
            while j < subtree.endIndex, subtree[j].indent > tileIndent { j += 1 }
            if let participant = participant(in: subtree[(i + 1)..<j]) { out.append(participant) }
            i = j
        }
        // Толк держит две ступени метки: `active-speaker dominant` у того, кто говорит прямо сейчас,
        // и голый `active-speaker` у того, кто говорил только что. Активным считаем dominant;
        // голая метка идёт в ход, только когда dominant нет ни у кого.
        if out.contains(where: { $0.active && !$0.recentOnly }) {
            for i in out.indices where out[i].recentOnly { out[i].active = false }
        }
        return TalkSnapshot(participants: out.map { var p = $0; p.recentOnly = false; return p })
    }

    /// One tile's rows (everything below the `grid-participant` group) turned into a participant,
    /// or nil when the tile has no name.
    private static func participant(in rows: ArraySlice<Row>) -> TalkParticipant? {
        var name: String?
        var fallbackName: String?
        var nameGroupIndent: Int?
        var micOn = false
        var active = false
        var dominant = false
        for row in rows {
            let list = classes(of: row.text)
            if list.contains(where: { $0.contains("active-speaker") }) { active = true }
            if list.contains("dominant") { dominant = true }
            if name == nil, row.text.hasPrefix("AXButton"), list.contains("participant-info-content"),
               let title = capture(titlePattern, in: row.text), !title.isEmpty {
                name = title
            }
            if row.text.hasPrefix("AXButton"), list.contains("participant-icon") {
                micOn = !list.contains("mute")
            }
            if list.contains("participant-name") { nameGroupIndent = row.indent }
            if fallbackName == nil, row.text.hasPrefix("AXStaticText"), let nameIndent = nameGroupIndent,
               row.indent > nameIndent, let value = capture(valuePattern, in: row.text), !value.isEmpty {
                fallbackName = value
            }
        }
        guard let resolved = name ?? fallbackName else { return nil }
        var p = TalkParticipant(name: resolved, micOn: micOn, active: active)
        p.recentOnly = active && !dominant
        return p
    }

    // MARK: - Lines

    /// A dump line split into its indentation and its content. Bookkeeping lines (`pid …`, `--- …`)
    /// carry no indentation of their own and would break the nesting, so they never become rows.
    struct Row: Equatable {
        var indent: Int
        var text: String
    }

    static func rows(of dump: String) -> [Row] {
        dump.split(separator: "\n", omittingEmptySubsequences: true).compactMap { raw in
            let indent = raw.prefix(while: { $0 == " " }).count
            let text = String(raw.dropFirst(indent))
            guard !text.isEmpty, !text.hasPrefix("---"), !text.hasPrefix("pid ") else { return nil }
            return Row(indent: indent, text: text)
        }
    }

    /// The `class='a b c'` of a line, as tokens. Empty for a line that has none.
    static func classes(of line: String) -> [String] {
        guard let raw = capture(classPattern, in: line) else { return [] }
        return raw.split(separator: " ").map(String.init)
    }

    private static func capture(_ pattern: NSRegularExpression, in line: String) -> String? {
        let range = NSRange(line.startIndex..., in: line)
        guard let m = pattern.firstMatch(in: line, range: range), m.numberOfRanges > 1,
              let r = Range(m.range(at: 1), in: line) else { return nil }
        return String(line[r])
    }

    // MARK: - JSON Lines

    /// One line of `talk.jsonl`. Written by hand rather than by `JSONEncoder` so the key order is
    /// the documented one and the names stay readable UTF-8 — the file is meant to be `tail`-ed.
    public func jsonLine(t seconds: TimeInterval) -> String {
        let stamp = String(format: "%.1f", max(seconds, 0))
        let items = participants.map {
            "{\"name\": \"\(Self.escape($0.name))\", \"mic\": \($0.micOn), \"active\": \($0.active)}"
        }
        return "{\"t\": \(stamp), \"participants\": [\(items.joined(separator: ", "))]}"
    }

    static func escape(_ s: String) -> String {
        var out = ""
        for ch in s.unicodeScalars {
            switch ch {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if ch.value < 0x20 {
                    out += String(format: "\\u%04x", ch.value)
                } else {
                    out.unicodeScalars.append(ch)
                }
            }
        }
        return out
    }
}

/// Watches the Толк grid for the whole of a recording and writes down every change: who is in the
/// call, whose mic is on, who is speaking. The result is `talk.jsonl` next to the audio, which the
/// worker can line up against the transcript to name the speakers.
///
/// Read-only, like `AXRecon`: it samples the Accessibility tree and never touches the app.
///
/// Threading: every field below is touched on `queue` alone, except `lastSnapshot`, which the
/// `--talk-test` entry point reads from another thread under `lock`. `start` and `stop` may be
/// called from anywhere and do nothing but hand work to `queue`.
public final class TalkObserver {
    /// A dump this fast is cheap enough to take every second.
    public static let fastSample: TimeInterval = 0.4
    /// Slower than this and the dump is competing with the recording for CPU.
    public static let slowSample: TimeInterval = 0.9
    public static let fastInterval: TimeInterval = 1
    public static let mediumInterval: TimeInterval = 2
    public static let slowInterval: TimeInterval = 3
    /// Even an unchanged grid gets a line this often, so a gap in the file means «the observer was
    /// not running», not «nothing happened».
    public static let heartbeat: TimeInterval = 30
    /// Chrome builds the web-content tree only after `enableEnhancedAccessibility`.
    public static let firstSampleDelay: TimeInterval = 1
    /// No participants group for this long from the start of the session is worth one warning.
    public static let quietWarningAfter: TimeInterval = 60
    public static let maxNodes = 8000
    public static let maxDepth = 60

    public let url: URL
    private let pid: pid_t
    private let startedAt: Date
    private let log: Log
    private let queue = DispatchQueue(label: "transcriber.talk-observer", qos: .utility)
    private let lock = NSLock()

    private var samples = 0
    private var written = 0
    private var previous: TalkSnapshot?
    private var lastWriteAt: Date?
    private var sawParticipants = false
    private var warnedQuiet = false
    private var handle: FileHandle?
    private var stopRequested = false
    private var finished = false
    private var _lastSnapshot: TalkSnapshot?

    /// `startedAt` is the start of the *session*, so `t` lines up with the audio the worker
    /// transcribes.
    public init(pid: pid_t, sessionDir: SessionDir, startedAt: Date = Date(), log: Log = .shared) {
        self.pid = pid
        self.startedAt = startedAt
        self.url = sessionDir.url.appendingPathComponent("talk.jsonl")
        self.log = log
    }

    /// The most recent parsed grid, for `--talk-test` and anything that wants to show it live.
    public var lastSnapshot: TalkSnapshot? {
        lock.lock()
        defer { lock.unlock() }
        return _lastSnapshot
    }

    public func start() {
        queue.async { [weak self] in
            guard let self else { return }
            AXDump.enableEnhancedAccessibility(AXUIElementCreateApplication(pid))
            log.info("talk observer started (pid \(pid)), \(url.path)")
        }
        queue.asyncAfter(deadline: .now() + Self.firstSampleDelay) { [weak self] in self?.step() }
    }

    /// Called when the session stops. Safe to call more than once and from any thread.
    public func stop() {
        lock.lock()
        stopRequested = true
        lock.unlock()
        queue.async { [weak self] in self?.finish() }
    }

    private var isStopped: Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopRequested
    }

    private func step() {
        guard !finished, !isStopped else { finish(); return }
        let begun = Date()
        let text = AXDump.dump(pid: pid, maxNodes: Self.maxNodes, maxDepth: Self.maxDepth)
        let elapsed = Date().timeIntervalSince(begun)
        guard !text.hasPrefix("accessibility not granted") else {
            log.warn("talk observer: accessibility not granted, stopping")
            finish()
            return
        }
        samples += 1
        record(TalkSnapshot.parse(text), at: Date())
        let interval: TimeInterval = elapsed < Self.fastSample ? Self.fastInterval
            : (elapsed <= Self.slowSample ? Self.mediumInterval : Self.slowInterval)
        queue.asyncAfter(deadline: .now() + interval) { [weak self] in self?.step() }
    }

    /// A sample becomes a line only when the grid actually differs from the last one written, or
    /// when the heartbeat is due. `nil` — no participants group — is not a state worth writing: it
    /// is the normal look of a tab that is not the call.
    private func record(_ snapshot: TalkSnapshot?, at now: Date) {
        guard let snapshot else {
            if !sawParticipants, !warnedQuiet,
               now.timeIntervalSince(startedAt) >= Self.quietWarningAfter {
                warnedQuiet = true
                log.warn("talk observer: no participants group; is the Толк tab in front?")
            }
            return
        }
        sawParticipants = true
        lock.lock()
        _lastSnapshot = snapshot
        lock.unlock()
        let due = lastWriteAt.map { now.timeIntervalSince($0) >= Self.heartbeat } ?? true
        guard snapshot != previous || due else { return }
        previous = snapshot
        lastWriteAt = now
        append(snapshot.jsonLine(t: now.timeIntervalSince(startedAt)) + "\n")
        written += 1
    }

    /// `queue` only. The handle stays open for the session and every line is flushed to disk at
    /// once: a session that ends in a crash must still leave a usable file.
    private func append(_ text: String) {
        guard let data = text.data(using: .utf8) else { return }
        if handle == nil {
            if !FileManager.default.fileExists(atPath: url.path) {
                FileManager.default.createFile(atPath: url.path, contents: nil)
            }
            handle = try? FileHandle(forWritingTo: url)
            try? handle?.seekToEnd()
        }
        guard let handle else { return }
        try? handle.write(contentsOf: data)
        try? handle.synchronize()
    }

    private func finish() {
        guard !finished else { return }
        finished = true
        try? handle?.close()
        handle = nil
        let last = previous?.participants.count ?? 0
        log.info("talk observer: \(samples) samples, \(written) written, last participants: \(last)")
    }
}
