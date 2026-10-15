import Foundation

public enum MeetingEvent: Equatable {
    case started(app: String)
    case ended
}

/// Polled state machine: a meeting starts when a watched app records from the mic, and ends
/// when no watched app has used the mic for `endGrace` seconds.
public final class MeetingDetector {
    /// Browsers are watched too: a Толк/Meet call in a tab shows up only as the browser's
    /// audio helper holding the mic, and the first missed meeting was exactly that.
    public static let defaultApps = ["kontur.talk", "us.zoom", "com.google.Chrome",
                                     "com.microsoft.edgemac", "com.apple.Safari"]

    private let apps: [String]
    private let lister: () -> [AudioProcess]
    private let endGrace: TimeInterval
    public private(set) var activeApp: String?
    private var micLostAt: Date?

    public init(apps: [String] = MeetingDetector.defaultApps, lister: @escaping () -> [AudioProcess],
                endGrace: TimeInterval = 60) {
        self.apps = apps
        self.lister = lister
        self.endGrace = endGrace
    }

    /// Returns the watched app (by prefix) that currently records from the mic, if any.
    private func appUsingMic(in processes: [AudioProcess]) -> String? {
        for p in processes where p.isRunningInput {
            if let app = apps.first(where: { p.bundleID.hasPrefix($0) }) { return app }
        }
        return nil
    }

    /// Two accepted limitations:
    /// (a) if the active app switches within a single poll window, `activeApp` keeps the app
    ///     that was first seen using the mic and does not update to the newer one;
    /// (b) any watched app touching the mic outside an actual call (e.g. a Zoom mic test)
    ///     triggers a `.started` event — the user simply declines the prompt.
    public func poll(now: Date) -> MeetingEvent? {
        poll(now: now, processes: lister())
    }

    /// Same, over an already fetched process list — the caller (AppDelegate) also needs it for
    /// the mic-holder diagnostics and should not pay for a second Core Audio round trip.
    public func poll(now: Date, processes: [AudioProcess]) -> MeetingEvent? {
        let using = appUsingMic(in: processes)
        if activeApp == nil {
            guard let app = using else { return nil }
            activeApp = app
            micLostAt = nil
            return .started(app: app)
        }
        if using != nil {
            micLostAt = nil
            return nil
        }
        if let lost = micLostAt {
            if now.timeIntervalSince(lost) >= endGrace {
                activeApp = nil
                micLostAt = nil
                return .ended
            }
            return nil
        }
        micLostAt = now
        return nil
    }

    public func reset() {
        activeApp = nil
        micLostAt = nil
    }
}
