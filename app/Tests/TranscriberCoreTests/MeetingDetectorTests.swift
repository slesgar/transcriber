import Foundation
import Testing
@testable import TranscriberCore

private func proc(_ bundle: String, input: Bool) -> AudioProcess {
    AudioProcess(objectID: 1, pid: 1, bundleID: bundle, isRunningInput: input)
}

@Test func startsWhenWatchedAppUsesMic() {
    var procs: [AudioProcess] = [proc("com.google.Chrome.helper", input: false)]
    let d = MeetingDetector(apps: ["kontur.talk", "us.zoom"], lister: { procs }, endGrace: 60)
    let t0 = Date(timeIntervalSince1970: 1_000)
    #expect(d.poll(now: t0) == nil)
    procs.append(proc("kontur.talk.helper", input: true))
    #expect(d.poll(now: t0 + 2) == .started(app: "kontur.talk"))
    #expect(d.poll(now: t0 + 4) == nil)                       // no repeat while active
    #expect(d.activeApp == "kontur.talk")
}

@Test func endsAfterGracePeriodWithoutMic() {
    var input = true
    let d = MeetingDetector(apps: ["kontur.talk"], lister: { [proc("kontur.talk.helper", input: input)] }, endGrace: 60)
    let t0 = Date(timeIntervalSince1970: 1_000)
    #expect(d.poll(now: t0) == .started(app: "kontur.talk"))
    input = false
    #expect(d.poll(now: t0 + 10) == nil)                      // grace running
    input = true
    #expect(d.poll(now: t0 + 30) == nil)                      // mic back: grace cancelled
    input = false
    #expect(d.poll(now: t0 + 40) == nil)
    #expect(d.poll(now: t0 + 99) == nil)
    #expect(d.poll(now: t0 + 101) == .ended)
    #expect(d.activeApp == nil)
    #expect(d.poll(now: t0 + 103) == nil)
}

@Test func unwatchedAppsAreIgnored() {
    let d = MeetingDetector(lister: { [proc("com.apple.Music", input: true)] })
    #expect(d.poll(now: Date()) == nil)
}

/// The first missed meeting ran in a browser tab (Толк web), so browsers are watched too —
/// their audio helper is what actually holds the mic, hence the prefix match.
@Test func browsersAreWatchedByDefault() {
    #expect(MeetingDetector.defaultApps == ["kontur.talk", "us.zoom", "com.google.Chrome",
                                            "com.microsoft.edgemac", "com.apple.Safari"])
    let d = MeetingDetector(lister: { [proc("com.google.Chrome.helper", input: true)] })
    #expect(d.poll(now: Date()) == .started(app: "com.google.Chrome"))
}

@Test func resetForcesIdle() {
    let d = MeetingDetector(apps: ["kontur.talk"], lister: { [proc("kontur.talk", input: true)] }, endGrace: 60)
    _ = d.poll(now: Date())
    d.reset()
    #expect(d.activeApp == nil)
    #expect(d.poll(now: Date()) == .started(app: "kontur.talk"))
}
