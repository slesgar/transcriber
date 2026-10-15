import ApplicationServices
import Foundation

/// One line that appeared or disappeared between two samples of the AX tree.
public struct AXChange: Equatable {
    public enum Kind: String { case added = "+", removed = "-" }
    public var kind: Kind
    public var line: String
    /// How many copies of an identical line appeared or disappeared — a call with eight tiles has
    /// eight identical `AXGroup` lines, and «three of them went away» is the interesting part.
    public var count: Int

    public init(kind: Kind, line: String, count: Int = 1) {
        self.kind = kind
        self.line = line
        self.count = count
    }
}

/// Everything the reconnaissance does to text, kept free of Accessibility so it can be tested
/// against literal dumps.
public enum AXReconText {
    /// `AXNodeRecord.line` always ends with the frame, if it has one.
    private static let framePattern = try! NSRegularExpression(
        pattern: #"\s*frame=-?\d+,-?\d+ -?\d+x-?\d+$"#)
    private static let valuePattern = try! NSRegularExpression(pattern: #"value='(.*?)'"#)

    /// A dump line reduced to what is worth comparing between two samples: no leading indentation
    /// and no frame. A video tile that shifts by a pixel, or a node that moves one level deeper
    /// when a panel opens, is not the change we are hunting — the one we want is a class or a
    /// description flipping when the speaker changes.
    public static func normalize(_ line: String) -> String {
        let range = NSRange(line.startIndex..., in: line)
        let noFrame = framePattern.stringByReplacingMatches(in: line, range: range, withTemplate: "")
        return noFrame.trimmingCharacters(in: .whitespaces)
    }

    /// Node lines of a dump, normalized. The `pid …` header and the `--- …` separators are
    /// bookkeeping, not nodes: `--- nodes N` moves on almost every sample and would drown the
    /// change log in noise.
    public static func normalizedLines(from dump: String) -> [String] {
        dump.split(separator: "\n", omittingEmptySubsequences: true).compactMap { raw in
            let head = raw.drop(while: { $0 == " " })
            guard !head.hasPrefix("---"), !head.hasPrefix("pid ") else { return nil }
            let normalized = normalize(String(raw))
            return normalized.isEmpty ? nil : normalized
        }
    }

    /// Multiset difference: a line present three times before and once now counts as two removals,
    /// so a tile disappearing is visible even when its neighbours are identical to it.
    /// Added lines keep the order of `current`, removed ones the order of `previous`.
    public static func diff(from previous: [String], to current: [String]) -> [AXChange] {
        var before = counts(previous)
        var after = counts(current)
        var out: [AXChange] = []
        for line in distinct(current) {
            let delta = (after[line] ?? 0) - (before[line] ?? 0)
            if delta > 0 { out.append(AXChange(kind: .added, line: line, count: delta)) }
        }
        for line in distinct(previous) {
            let delta = (before[line] ?? 0) - (after[line] ?? 0)
            if delta > 0 { out.append(AXChange(kind: .removed, line: line, count: delta)) }
        }
        before.removeAll()
        after.removeAll()
        return out
    }

    /// How many nodes carry each role, from normalized lines: the role is the first token of
    /// `AXNodeRecord.line`.
    public static func roleCounts(from normalized: [String]) -> [String: Int] {
        var out: [String: Int] = [:]
        for line in normalized {
            let role = String(line.prefix(while: { $0 != " " }))
            guard !role.isEmpty else { continue }
            out[role, default: 0] += 1
        }
        return out
    }

    /// The `value='…'` of every `AXStaticText`, in order, deduplicated — the pool the participant
    /// list is fished out of.
    public static func staticTextValues(from normalized: [String]) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for line in normalized where line.hasPrefix("AXStaticText") {
            let range = NSRange(line.startIndex..., in: line)
            guard let m = valuePattern.firstMatch(in: line, range: range), m.numberOfRanges > 1,
                  let r = Range(m.range(at: 1), in: line) else { continue }
            let value = String(line[r])
            guard !value.isEmpty, seen.insert(value).inserted else { continue }
            out.append(value)
        }
        return out
    }

    /// `mm:ss.s` from the start of the session. Computed in tenths so 59.97 s never prints `:60.0`.
    public static func stamp(_ seconds: TimeInterval) -> String {
        let tenths = Int((max(seconds, 0) * 10).rounded())
        return String(format: "%02d:%02d.%d", tenths / 600, (tenths % 600) / 10, tenths % 10)
    }

    /// One line of `changes.log`.
    public static func changeLine(_ change: AXChange, at seconds: TimeInterval) -> String {
        let repeated = change.count > 1 ? "\(change.count)× " : ""
        return "\(stamp(seconds)) \(change.kind.rawValue) \(repeated)\(change.line)"
    }

    /// What one recon run learned, accumulated while it ran.
    public struct Stats: Equatable {
        public var samples = 0
        public var snapshots = 0
        public var changeEvents = 0
        public var duration: TimeInterval = 0
        /// From the last sample: a representative tree, not a sum over all of them.
        public var roleCounts: [String: Int] = [:]
        /// How many change events each normalized line took part in.
        public var changeTally: [String: Int] = [:]
        /// Every `AXStaticText` value seen across the whole run, in order of first appearance.
        public var staticTexts: [String] = []

        public init() {}
    }

    public static let topChangingLimit = 30

    public static func summary(sessionID: String, stats: Stats) -> String {
        var out: [String] = []
        out.append("recon сессии \(sessionID)")
        out.append("сэмплов \(stats.samples), снимков \(stats.snapshots), событий изменения "
                   + "\(stats.changeEvents), длительность \(stamp(stats.duration))")
        out.append("")
        out.append("== узлы по ролям (последний сэмпл) ==")
        let roles = stats.roleCounts.sorted { ($0.value, $1.key) > ($1.value, $0.key) }
        for (role, count) in roles { out.append("\(count)\t\(role)") }
        if roles.isEmpty { out.append("—") }
        out.append("")
        out.append("== чаще всего меняются (топ \(topChangingLimit)) ==")
        let top = stats.changeTally.sorted { ($0.value, $1.key) > ($1.value, $0.key) }
            .prefix(topChangingLimit)
        for (line, count) in top { out.append("\(count)\t\(line)") }
        if top.isEmpty { out.append("—") }
        out.append("")
        let names = TalkInspector.participantNames(from: stats.staticTexts)
        out.append("== кандидаты в участники (\(names.count) из \(stats.staticTexts.count) AXStaticText) ==")
        for name in names { out.append(name) }
        if names.isEmpty { out.append("—") }
        return out.joined(separator: "\n") + "\n"
    }

    private static func counts(_ lines: [String]) -> [String: Int] {
        var out: [String: Int] = [:]
        for line in lines { out[line, default: 0] += 1 }
        return out
    }

    private static func distinct(_ lines: [String]) -> [String] {
        var seen = Set<String>()
        return lines.filter { seen.insert($0).inserted }
    }
}

/// Samples the Accessibility tree of one app for the first minutes of a recording and writes down
/// what moved. Read-only reconnaissance: it never clicks, never parses the tree into meaning, and
/// exists only so the next real call in Толк leaves enough evidence to build a speaker detector on.
///
/// Threading: every field below is touched on `queue` alone. `start` and `stop` are the only
/// members a caller may reach from the main thread, and both do nothing but hand work to `queue`.
public final class AXRecon {
    public static var defaultRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/transcriber-ax")
    }

    /// Enough to see a whole call's worth of change without filling the disk with near-identical
    /// trees; the change log carries the rest.
    public static let maxSnapshots = 40
    /// A dump slower than this is competing with the recording for CPU — take fewer of them.
    public static let slowSample: TimeInterval = 0.7
    public static let interval: TimeInterval = 1
    public static let backoffInterval: TimeInterval = 3
    /// Chrome and Electron build the web-content tree only after `enableEnhancedAccessibility`;
    /// sampling instantly would capture a stub of a dozen nodes.
    public static let firstSampleDelay: TimeInterval = 1
    public static let maxNodes = 6000
    public static let maxDepth = 50

    public let dir: URL
    private let pid: pid_t
    private let sessionID: String
    private let startedAt: Date
    private let deadline: Date
    private let log: Log
    private let queue = DispatchQueue(label: "transcriber.ax-recon", qos: .utility)
    private let stopLock = NSLock()
    private var stopRequested = false

    private var stats = AXReconText.Stats()
    private var sampleInterval = AXRecon.interval
    private var previousDump: String?
    private var previousLines: [String] = []
    private var seenTexts = Set<String>()
    private var finished = false

    /// `startedAt` is the start of the *session*, so the timestamps in `changes.log` line up with
    /// the audio the worker transcribes.
    public init(pid: pid_t, sessionID: String, startedAt: Date = Date(), seconds: Double,
                root: URL = AXRecon.defaultRoot, log: Log = .shared) {
        self.pid = pid
        self.sessionID = sessionID
        self.startedAt = startedAt
        self.deadline = Date().addingTimeInterval(max(seconds, 0))
        self.dir = root.appendingPathComponent(sessionID, isDirectory: true)
        self.log = log
    }

    public var changesURL: URL { dir.appendingPathComponent("changes.log") }
    public var summaryURL: URL { dir.appendingPathComponent("summary.txt") }

    public func start() {
        let seconds = deadline.timeIntervalSinceNow
        queue.async { [weak self] in
            guard let self else { return }
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            AXDump.enableEnhancedAccessibility(AXUIElementCreateApplication(pid))
            log.info("ax recon started for \(pid), \(Int(seconds.rounded()))s, \(dir.path)")
        }
        queue.asyncAfter(deadline: .now() + Self.firstSampleDelay) { [weak self] in self?.step() }
    }

    /// Called when the session stops. Safe to call more than once and from any thread.
    public func stop() {
        stopLock.lock()
        stopRequested = true
        stopLock.unlock()
        queue.async { [weak self] in self?.finish() }
    }

    private var isStopped: Bool {
        stopLock.lock()
        defer { stopLock.unlock() }
        return stopRequested
    }

    private func step() {
        guard !finished else { return }
        guard !isStopped, Date() < deadline else { finish(); return }
        let begun = Date()
        let text = AXDump.dump(pid: pid, maxNodes: Self.maxNodes, maxDepth: Self.maxDepth)
        let elapsed = Date().timeIntervalSince(begun)
        guard !text.hasPrefix("accessibility not granted") else {
            log.warn("ax recon: accessibility not granted, stopping")
            finish()
            return
        }
        if elapsed > Self.slowSample, sampleInterval < Self.backoffInterval {
            sampleInterval = Self.backoffInterval
            log.info("ax recon: sample took \(Int(elapsed * 1000)) ms, sampling every \(Int(sampleInterval)) s")
        }
        record(text, at: Date())
        queue.asyncAfter(deadline: .now() + sampleInterval) { [weak self] in self?.step() }
    }

    private func record(_ text: String, at now: Date) {
        let first = stats.samples == 0
        stats.samples += 1
        if text != previousDump {
            previousDump = text
            if stats.snapshots < Self.maxSnapshots {
                stats.snapshots += 1
                try? text.write(to: dir.appendingPathComponent("snap-\(stats.snapshots).txt"),
                                atomically: true, encoding: .utf8)
            }
        }
        let lines = AXReconText.normalizedLines(from: text)
        stats.roleCounts = AXReconText.roleCounts(from: lines)
        for value in AXReconText.staticTextValues(from: lines) where seenTexts.insert(value).inserted {
            stats.staticTexts.append(value)
        }
        defer { previousLines = lines }
        guard !first else { return }
        let changes = AXReconText.diff(from: previousLines, to: lines)
        guard !changes.isEmpty else { return }
        let seconds = now.timeIntervalSince(startedAt)
        var out = ""
        for change in changes {
            stats.changeEvents += 1
            stats.changeTally[change.line, default: 0] += 1
            out += AXReconText.changeLine(change, at: seconds) + "\n"
        }
        append(out, to: changesURL)
    }

    private func finish() {
        guard !finished else { return }
        finished = true
        stats.duration = Date().timeIntervalSince(startedAt)
        let text = AXReconText.summary(sessionID: sessionID, stats: stats)
        try? text.write(to: summaryURL, atomically: true, encoding: .utf8)
        log.info("ax recon: \(stats.snapshots) snapshots, \(stats.changeEvents) change events, "
                 + "summary at \(summaryURL.path)")
    }

    /// `queue` only, so no locking beyond what the file system gives us.
    private func append(_ text: String, to url: URL) {
        guard let data = text.data(using: .utf8) else { return }
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: url, options: .atomic)
        }
    }
}
