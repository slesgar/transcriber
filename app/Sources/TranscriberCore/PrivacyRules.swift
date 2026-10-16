import Foundation

/// A recurring private slot in the week (a regular personal appointment), always at the same
/// hour. `weekday` follows `Calendar`: 1 = Sunday … 7 = Saturday.
/// `from`/`to` are local wall-clock times, `"HH:mm"`.
public struct PrivateWindow: Codable, Equatable {
    public var weekday: Int
    public var from: String
    public var to: String

    public init(weekday: Int, from: String, to: String) {
        self.weekday = weekday
        self.from = from
        self.to = to
    }

    /// One line of the settings window: `"<weekday 1–7> HH:mm-HH:mm"`, e.g. `3 12:00-12:50`.
    /// Spaces around the parts are tolerated; anything else (a weekday outside 1…7, a bad time)
    /// is nil, so the window can show the owner exactly which lines it rejected.
    public static func parse(line: String) -> PrivateWindow? {
        let parts = line.split(whereSeparator: { $0 == " " || $0 == "\t" })
        guard let dayText = parts.first, let weekday = Int(dayText), (1...7).contains(weekday) else { return nil }
        let range = parts.dropFirst().joined().split(separator: "-", omittingEmptySubsequences: false)
        guard range.count == 2 else { return nil }
        let from = String(range[0]), to = String(range[1])
        guard PrivacyRules.minutes(from) != nil, PrivacyRules.minutes(to) != nil else { return nil }
        return PrivateWindow(weekday: weekday, from: from, to: to)
    }

    /// The inverse of `parse(line:)`.
    public var line: String { "\(weekday) \(from)-\(to)" }
}

/// What must never reach the work vault. Two independent triggers, either one is enough:
/// a bundle-ID prefix (a private call happens in Zoom) and a weekly time window. A session
/// marked private tells the worker to write the transcript into the private folder instead of
/// the vault. The defaults are neutral: no windows, no apps — the owner fills them in.
///
/// Both ends of a window are inclusive and the windows are padded around the real appointment:
/// the cost of a false positive is one note in the wrong folder, the cost of a false negative is
/// a private-call transcript in a work vault. The padding is what makes the work exemption below
/// necessary: a work call that starts inside the padding is still a work call.
public struct PrivacyRules: Codable, Equatable {
    public var windows: [PrivateWindow]
    public var apps: [String]
    /// Толк is work by definition: a private call is never held there and every work meeting is. A
    /// bundle-ID prefix from this list therefore beats both triggers above.
    public var workApps: [String]
    /// The same exemption for a call in a browser tab, where the app is merely «Chrome» — the
    /// host of the open call tab is the only thing that can tell the two apart. Matched by
    /// `BrowserTabs.isWorkMeeting`, which hands the answer in as `workContext`.
    public var workHosts: [String]

    public init(windows: [PrivateWindow], apps: [String],
                workApps: [String] = PrivacyRules.defaultWorkApps,
                workHosts: [String] = PrivacyRules.defaultWorkHosts) {
        self.windows = windows
        self.apps = apps
        self.workApps = workApps
        self.workHosts = workHosts
    }

    public static let defaultWorkApps = ["kontur.talk"]
    public static let defaultWorkHosts: [String] = []

    public static let defaults = PrivacyRules(windows: [], apps: [])

    /// The two work keys were added after the file was already on disk, so a config without them
    /// is not broken — it simply has no opinion, and the defaults keep Толк out of the private
    /// folder. `windows` and `apps` stay required: a file that lost those is malformed, and
    /// `load` falls back to the defaults, which is the safe direction.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        windows = try c.decode([PrivateWindow].self, forKey: .windows)
        apps = try c.decode([String].self, forKey: .apps)
        workApps = try c.decodeIfPresent([String].self, forKey: .workApps) ?? PrivacyRules.defaultWorkApps
        workHosts = try c.decodeIfPresent([String].self, forKey: .workHosts) ?? PrivacyRules.defaultWorkHosts
    }

    /// `workContext` is the caller's answer to «this is a work call» — for a browser, a call tab
    /// on one of `workHosts`. Толк never carries a private call, so it outranks everything else here.
    public func isPrivate(app: String, at date: Date, workContext: Bool = false,
                          calendar: Calendar = .current) -> Bool {
        if workContext || isWorkApp(app) { return false }
        return isPrivateApp(app) || isInPrivateWindow(at: date, calendar: calendar)
    }

    public func isWorkApp(_ app: String) -> Bool {
        workApps.contains { !$0.isEmpty && app.hasPrefix($0) }
    }

    public func isPrivateApp(_ app: String) -> Bool {
        apps.contains { !$0.isEmpty && app.hasPrefix($0) }
    }

    /// The schedule half of `isPrivate`, on its own: a caller that is about to pay for an
    /// AppleScript round trip needs to know whether the answer could even matter.
    public func isInPrivateWindow(at date: Date, calendar: Calendar = .current) -> Bool {
        let parts = calendar.dateComponents([.weekday, .hour, .minute], from: date)
        guard let weekday = parts.weekday, let hour = parts.hour, let minute = parts.minute else { return false }
        let now = hour * 60 + minute
        return windows.contains { window in
            guard window.weekday == weekday,
                  let from = Self.minutes(window.from), let to = Self.minutes(window.to) else { return false }
            // A window that wraps past midnight («23:30»–«00:30») is two ranges, not one.
            return from <= to ? (now >= from && now <= to) : (now >= from || now <= to)
        }
    }

    /// Minutes since midnight for `"HH:mm"`; nil for anything else, so a typo in the config
    /// disables that one window instead of throwing the whole file away.
    static func minutes(_ text: String) -> Int? {
        let parts = text.split(separator: ":")
        guard parts.count == 2, let h = Int(parts[0]), let m = Int(parts[1]),
              (0...23).contains(h), (0...59).contains(m) else { return nil }
        return h * 60 + m
    }

    // MARK: - Config file

    /// The owner edits the schedule here; the app re-reads it at every session start.
    public static var defaultURL: URL { AppPaths.appConfig }

    /// Rules from the `privacy` key of the JSON at `url`. A missing file is written out with the
    /// defaults (so there is something to edit); a broken one is ignored — falling back to the
    /// defaults keeps a private call private, which is the whole point of the file.
    public static func load(from url: URL = PrivacyRules.defaultURL) -> PrivacyRules {
        guard let data = try? Data(contentsOf: url) else {
            writeDefaults(to: url)
            return defaults
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            invalidWarned.fire { Log.shared.warn("privacy rules: \(url.path) is not valid JSON, using defaults") }
            return defaults
        }
        guard let privacy = object["privacy"] else { return defaults }
        guard let sub = try? JSONSerialization.data(withJSONObject: privacy),
              let rules = try? JSONDecoder().decode(PrivacyRules.self, from: sub) else {
            invalidWarned.fire { Log.shared.warn("privacy rules: 'privacy' in \(url.path) is malformed, using defaults") }
            return defaults
        }
        return rules
    }

    private static func writeDefaults(to url: URL) {
        guard let encoded = try? JSONEncoder().encode(defaults),
              let privacy = try? JSONSerialization.jsonObject(with: encoded) else { return }
        let payload: [String: Any] = [
            "_comment": "privacy.windows: weekday 1=вс … 7=сб, время локальное HH:mm, обе границы включительно. "
                + "privacy.apps — префиксы bundle id. Запись, попавшая в окно или в это приложение, уходит в "
                + "папку приватных записей (private_dir), а не в рабочий вольт. Толк никогда не приватный: privacy.workApps "
                + "(префиксы bundle id) и privacy.workHosts (хосты звонков в браузере) перебивают и окно, "
                + "и privacy.apps.",
            "privacy": privacy,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload,
                                                     options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        else { return }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url, options: .atomic)
    }

    /// The rules are re-read at every session start, so a broken file would otherwise fill the log
    /// with the same line all day.
    private static let invalidWarned = WarnOnce()

    private final class WarnOnce {
        private let lock = NSLock()
        private var fired = false
        func fire(_ body: () -> Void) {
            lock.lock()
            let first = !fired
            fired = true
            lock.unlock()
            if first { body() }
        }
    }
}
