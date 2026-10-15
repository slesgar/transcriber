import Foundation
import TranscriberCore

/// Owns one recording at a time: creates the session dir, holds power, runs capture,
/// downmixes both inputs and appends them to mic.pcm / sys.pcm, and writes STOP at the end.
///
/// Threading invariant (no locks needed beyond this):
/// writers/mixers are created before capture starts and torn down only after `TapCapture.stop()`
/// has drained its queue; `consume` is the only accessor on the capture queue.
/// The two mixers are the one exception to «created before capture starts»: their channel counts
/// are known only from the first chunk, so they are built inside `consume` itself — still on the
/// capture queue, still the only accessor. `capture`, `micWriter` and `sysWriter` are assigned
/// before `cap.start`, so the capture queue never observes them mutate once delivery begins;
/// `stop()` and `checkHealth()` touch them only after `capture.stop()`'s `queue.sync {}` barrier
/// has returned. `lastChunkAt` is the one field both threads write, so it carries its own lock.
final class SessionController {
    enum State { case idle, recording(SessionDir) }

    private(set) var state: State = .idle
    var onStateChange: ((State) -> Void)?
    var onError: ((String) -> Void)?

    /// True while the running session is private: the note lives outside the vault, so the UI has
    /// to open it differently and say so.
    private(set) var currentIsPrivate = false
    /// When the running session started and what it is called — the floating button's menu shows
    /// both. A private session keeps no title, exactly as everywhere else.
    private(set) var currentStartedAt: Date?
    private(set) var currentTitle: String?
    /// Bundle-ID prefix the running session was started for. The AX reconnaissance needs it to
    /// find the app whose tree to sample, and only the session knows which app the recording
    /// actually belongs to — `activeApp` in the delegate can already have moved on.
    private(set) var currentApp: String?
    /// One-shot override from the menu — forces the next session private whatever the schedule
    /// says, then clears itself.
    var forcePrivateNext = false

    private let store: SessionStore
    private let power = PowerHold()
    private var capture: TapCapture?
    private var micMixer: Downmixer?
    private var sysMixer: Downmixer?
    private var micWriter: PcmWriter?
    private var sysWriter: PcmWriter?
    private var warnedNoMixer = false
    private let log = Log.shared
    /// Written on the capture queue, read from the main thread by `checkHealth` — hence the lock.
    private let chunkLock = NSLock()
    private var lastChunkAtStorage = Date()
    private var lastChunkAt: Date {
        get { chunkLock.lock(); defer { chunkLock.unlock() }; return lastChunkAtStorage }
        set { chunkLock.lock(); lastChunkAtStorage = newValue; chunkLock.unlock() }
    }
    /// Last time either channel carried something louder than room noise. Same lock discipline.
    private var lastVoiceAtStorage = Date()
    private var lastVoiceAt: Date {
        get { chunkLock.lock(); defer { chunkLock.unlock() }; return lastVoiceAtStorage }
        set { chunkLock.lock(); lastVoiceAtStorage = newValue; chunkLock.unlock() }
    }
    static let micVoicePeak: Float = 0.05   // speech into the built-in mic is well above this
    static let sysVoicePeak: Float = 0.02   // remote speech through the tap

    /// Seconds since anyone spoke on either channel (0 when idle).
    func silentFor(now: Date) -> TimeInterval {
        guard current != nil else { return 0 }
        return now.timeIntervalSince(lastVoiceAt)
    }

    init(store: SessionStore = SessionStore(root: SessionStore.defaultRoot)) {
        self.store = store
    }

    var current: SessionDir? {
        if case .recording(let dir) = state { return dir }
        return nil
    }

    /// Would a session started now for `app` be private? Re-reads the rules, so the prompt the
    /// user sees and the session that follows always agree. `workContext` is the caller's answer
    /// to «this is a work call» — see `workContext(app:now:)`; the menu override still wins over
    /// everything.
    func wouldBePrivate(app: String, now: Date = Date(), workContext: Bool = false) -> Bool {
        forcePrivateNext || PrivacyRules.load().isPrivate(app: app, at: now, workContext: workContext)
    }

    /// Is a browser holding a work call right now? Inside a browser the bundle ID says nothing —
    /// only the open tab can tell a Толк meeting from a private hour. Reading the tabs costs an
    /// AppleScript round trip, so the question is asked only when the schedule alone is what
    /// would make the session private: an app match (Zoom) is not something a tab can undo, and
    /// outside the windows there is nothing to overturn.
    ///
    /// Touches no session state, so the poll queue can call it — and should: AppleScript has no
    /// business on the main thread.
    static func workContext(app: String, now: Date = Date()) -> Bool {
        let rules = PrivacyRules.load()
        guard AppDelegate.browserApps.contains(app),
              !rules.isWorkApp(app), !rules.isPrivateApp(app),
              rules.isInPrivateWindow(at: now) else { return false }
        return BrowserTabs.hasWorkMeeting(forBrowser: app, hosts: rules.workHosts)
    }

    /// `info` is for callers that already know the meeting (a browser tab, say); without it the
    /// only source is the Толк desktop window. `workContext` is the verdict from the detection
    /// that led here, passed along so the prompt and the session it starts cannot disagree.
    func start(app: String, info providedInfo: MeetingInfo? = nil, workContext: Bool = false,
               now: Date = Date()) {
        guard case .idle = state else { return }
        let isPrivate = wouldBePrivate(app: app, now: now, workContext: workContext)
        // A private session carries no title: the name of a private call has no business in a
        // file name, a log line or the menu — and the worker names the file by time alone.
        let info = isPrivate ? MeetingInfo()
                             : (providedInfo ?? (app == TalkInspector.bundleID ? TalkInspector.inspect() : MeetingInfo()))
        var created: SessionDir?
        do {
            let dir = try store.create(app: app, title: info.title, participants: info.participants,
                                       private: isPrivate)
            created = dir
            // Everything the capture queue reads is in place before the first chunk can arrive.
            micWriter = try PcmWriter(url: dir.micURL)
            sysWriter = try PcmWriter(url: dir.sysURL)
            micMixer = nil   // both mixers are built on the first chunk, once channel counts are known
            sysMixer = nil
            warnedNoMixer = false
            lastChunkAt = Date()
            lastVoiceAt = Date()
            let cap = TapCapture()
            capture = cap
            try cap.start { [weak self] chunk in self?.consume(chunk) }
            power.acquire()
            currentIsPrivate = isPrivate
            currentStartedAt = now
            currentTitle = isPrivate || info.title.isEmpty ? nil : info.title
            currentApp = app
            forcePrivateNext = false   // the override is spent only once the session really exists
            state = .recording(dir)
            log.info("session \(dir.id) started (app \(app), title '\(info.title)'\(isPrivate ? ", приватная" : ""))")
            onStateChange?(state)
        } catch {
            log.error("session start failed: \(error)")
            cleanup()
            if let dir = created { discardIfEmpty(dir) }
            currentIsPrivate = false
            currentStartedAt = nil
            currentTitle = nil
            currentApp = nil
            state = .idle
            onStateChange?(state)
            onError?("Не удалось начать запись: \(error)")
        }
    }

    func stop(reason: StopReason) {
        guard case .recording(let dir) = state else { return }
        capture?.stop()   // returns only after the capture queue has drained: mixers/writers are ours again
        if let m = micMixer { micWriter?.write(m.finish()) }
        if let s = sysMixer { sysWriter?.write(s.finish()) }
        let micSamples = micWriter?.samplesWritten ?? 0
        cleanup()
        do { try dir.requestStop() } catch { log.error("STOP write failed: \(error)") }
        power.release()
        currentIsPrivate = false
        currentStartedAt = nil
        currentTitle = nil
        currentApp = nil
        state = .idle
        log.info("session \(dir.id) stopped (\(reason.rawValue)); mic \(micSamples) samples")
        verifyCaptureReleased()
        onStateChange?(state)
    }

    /// Called from the AppDelegate tick and on wake. The tap dies silently when the aggregate
    /// device is rebuilt under us (sleep, a device switch) — the IOProc simply stops firing,
    /// so silence for longer than a few chunk intervals means the capture has to be rebuilt.
    func checkHealth(now: Date) {
        guard case .recording = state else { return }
        guard now.timeIntervalSince(lastChunkAt) > 5 else { return }
        restartCapture(reason: "no chunks for \(Int(now.timeIntervalSince(lastChunkAt)))s", now: now)
    }

    /// Rebuilds the tap under a running session. The stall watchdog uses it, and so does a change
    /// of the default input device: waiting the full five seconds there would mean five seconds of
    /// the old (now gone) mic in the file.
    func restartCapture(reason: String, now: Date = Date()) {
        guard case .recording(let dir) = state else { return }
        log.warn("capture restart for session \(dir.id): \(reason)")
        lastChunkAt = now   // also paces the retries if the restart below fails
        capture?.stop()     // returns after the capture queue drains: mixers/writers are ours again
        if let m = micMixer { micWriter?.write(m.finish()) }
        if let s = sysMixer { sysWriter?.write(s.finish()) }
        micMixer = nil      // rebuilt on the first chunk of the new capture, as at session start
        sysMixer = nil
        warnedNoMixer = false
        let cap = TapCapture()
        capture = cap
        do {
            try cap.start { [weak self] chunk in self?.consume(chunk) }
            log.info("capture restarted for session \(dir.id)")
        } catch {
            capture = nil
            log.error("capture restart failed for session \(dir.id): \(error)")
        }
    }

    /// Diagnostic only: after a stop Core Audio must no longer count us as recording. A
    /// lingering flag means a tap or aggregate device outlived the session (or the flag itself
    /// is stale) — either way we want it in the log. Off the main thread: it lists processes.
    private func verifyCaptureReleased() {
        let log = self.log
        DispatchQueue.global(qos: .utility).async {
            if AudioProcesses.selfIsRunningInput() {
                log.warn("still running input after stop")
            } else {
                log.info("capture released")
            }
        }
    }

    private func cleanup() {
        capture = nil
        micWriter?.close()
        sysWriter?.close()
        micWriter = nil
        sysWriter = nil
        micMixer = nil
        sysMixer = nil
    }

    /// A session dir with no audio in it would keep the worker waiting forever, so drop it.
    private func discardIfEmpty(_ dir: SessionDir) {
        guard fileSize(dir.micURL) == 0, fileSize(dir.sysURL) == 0 else { return }
        do {
            try FileManager.default.removeItem(at: dir.url)
            log.warn("session \(dir.id) removed: capture never started, nothing written")
        } catch {
            log.error("session \(dir.id) removal failed: \(error)")
        }
    }

    private func fileSize(_ url: URL) -> Int {
        ((try? FileManager.default.attributesOfItem(atPath: url.path))?[.size] as? Int) ?? 0
    }

    private func peak(_ samples: [Float]) -> Float {
        var m: Float = 0
        for x in samples where abs(x) > m { m = abs(x) }
        return m
    }

    /// Runs on the capture queue — the only place the mixers and writers are touched while recording.
    private func consume(_ chunk: CaptureChunk) {
        guard chunk.frames > 0, let capture else { return }
        lastChunkAt = Date()
        if peak(chunk.mic) > Self.micVoicePeak || peak(chunk.sys) > Self.sysVoicePeak {
            lastVoiceAt = Date()
        }
        if micMixer == nil {
            let channels = max(chunk.mic.count / chunk.frames, 1)
            micMixer = try? Downmixer(inputRate: capture.sampleRate, inputChannels: channels)
        }
        if sysMixer == nil {
            sysMixer = try? Downmixer(inputRate: capture.sampleRate, inputChannels: max(capture.sysChannels, 1))
        }
        // Both files are plain appends: writing one half of a chunk would shift that track against
        // the other for the rest of the session, so a missing mixer drops the whole chunk.
        guard let m = micMixer, let s = sysMixer else {
            if !warnedNoMixer {
                warnedNoMixer = true
                log.error("session: downmixer unavailable, dropping chunks (mic \(micMixer != nil), sys \(sysMixer != nil))")
            }
            return
        }
        micWriter?.write(m.process(interleaved: chunk.mic, frames: chunk.frames))
        sysWriter?.write(s.process(interleaved: chunk.sys, frames: chunk.frames))
    }
}
