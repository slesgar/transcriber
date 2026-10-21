import AppKit
import TranscriberCore

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let log = Log.shared
    private let status = StatusItem()
    private let prompt = PromptPanel()
    private let stopPrompt = PromptPanel()
    /// «Продолжить» on the silence card mutes it until someone speaks again.
    private var stopPromptMutedUntilVoice = false
    static let silencePromptAfter: TimeInterval = 180
    static let silenceStopAfter: TimeInterval = 600
    private let floating = FloatingButton()
    private let controller = SessionController()
    private let store = SessionStore(root: SessionStore.defaultRoot)
    private let detector = MeetingDetector(lister: AudioProcesses.list)
    private var pollTimer: Timer?
    private var termSource: DispatchSourceSignal?
    /// `detector` is touched only on `pollQueue`; the main thread works off this snapshot.
    private let pollQueue = DispatchQueue(label: "transcriber.poll")
    private var activeApp: String?
    private var declinedApp: String?   // «Не сейчас» for this meeting: don't ask again until it ends
    /// Browser holding the mic with nothing meeting-like open yet — we keep looking instead of
    /// asking about a YouTube tab. Main thread only.
    private var pendingBrowserApp: String?
    private var pendingTicks = 0
    /// Set while the pending tab is a call whose title is still a placeholder (see
    /// `BrowserTabs.MeetingTabState.placeholder`) — re-checked faster than a mic with no call tab
    /// at all, since the real title is expected any second.
    private var pendingIsPlaceholder = false
    /// When the placeholder title was first seen on the current pending app — nil once it clears
    /// or a real title lands. The safety net prompts anyway once this is old enough, for the rare
    /// room genuinely titled «Толк».
    private var placeholderSince: Date?
    /// Ticks are 2 s, so five of them is the ~10 s re-check of the browser's tabs.
    static let browserRecheckTicks = 5
    /// While the tab title is a placeholder the real one may land any second, so check more often.
    static let browserRecheckTicksPlaceholder = 2
    /// A call tab stuck on a placeholder title this long is more likely a room really named
    /// «Толк» than one still loading — prompt with the generic card rather than wait forever.
    static let placeholderSafetyNet: TimeInterval = 60
    /// Last logged set of mic holders; `pollQueue` only. `nil` = nothing logged yet.
    private var lastMicHolders: Set<String>?
    /// Keeps the Core Audio default-device listeners alive for the life of the app.
    private var deviceObserver: Any?
    /// The Accessibility reconnaissance of the current session, while one is running. Main thread
    /// only: `render` is the single place it is created and torn down.
    private var recon: AXRecon?
    /// The Толк grid watcher of the current session — unlike the recon it runs the whole session.
    /// Main thread only, like `recon`.
    private var talkObserver: TalkObserver?
    /// Both of the above are started by one lookup; this is what keeps it to one attempt per
    /// session even when only one of the two is switched on. Main thread only.
    private var axWatchersStarted = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        log.info("app launched")
        PowerHold.resetAtLaunch()   // a crash mid-session could have left sleep disabled
        // the owner's interactive shell takes seconds to report its PATH: ask it now, not when the
        // first question to the agent is already waiting
        DispatchQueue.global(qos: .utility).async { _ = ShellPath.discovered }
        _ = TalkInspector.ensureAccessibility()
        status.onToggle = { [weak self] in self?.toggle() }
        status.onOpenNote = { [weak self] in self?.openNote() }
        status.onOpenSettings = { SettingsWindow.shared.show() }
        status.onOpenSessions = { [weak self] in
            guard let self else { return }
            NSWorkspace.shared.open(self.store.root)
        }
        status.onTogglePrivateNext = { [weak self] in
            guard let self else { return }
            controller.forcePrivateNext.toggle()
            status.setPrivateNext(controller.forcePrivateNext)
            log.info("next session private: \(controller.forcePrivateNext)")
        }
        floating.onOpenNote = { [weak self] in self?.openNote() }
        floating.onStop = { [weak self] in self?.stopRecordingKeepingMeeting(reason: .manual) }
        floating.onHide = { [weak self] in
            self?.log.info("floating button hidden for this session")
            self?.floating.hide()
        }
        controller.onStateChange = { [weak self] state in
            DispatchQueue.main.async { self?.render(state) }
        }
        controller.onError = { [weak self] message in
            DispatchQueue.main.async { self?.status.setProblem(message) }
        }
        installTermHandler()
        observeAudioDevices()
        announceReconConfig()
        // .common, not the default mode: an open menu would otherwise freeze the detector.
        let timer = Timer(timeInterval: 2, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil,
                                                          queue: .main) { [weak self] _ in
            self?.log.info("woke from sleep")
            self?.controller.checkHealth(now: Date())   // the tap rarely survives sleep
            self?.poll()
        }
        poll()
    }

    /// Every switch of the default input or output goes into the log, session or no session: the
    /// question «почему звук не ушёл в AirPods» can only be answered against a record of what
    /// macOS actually did. We never touch routing — during a recording an input switch only makes
    /// the capture restart at once, instead of waiting out the five-second stall watchdog.
    private func observeAudioDevices() {
        let input = AudioDevices.defaultInput()
        let output = AudioDevices.defaultOutput()
        log.info("devices at launch: input '\(AudioDevices.name(of: input))', output '\(AudioDevices.name(of: output))'")
        deviceObserver = AudioDevices.observeDefaultDeviceChanges { [weak self] kind, name in
            guard let self else { return }
            log.info("default \(kind) changed → '\(name)'")
            guard kind == "input", controller.current != nil else { return }
            controller.restartCapture(reason: "default input changed to '\(name)'")
        }
    }

    /// launchd sends SIGTERM on «выход»/logout; the default disposition would kill us mid-session,
    /// leaving mic.pcm without a STOP file and the worker waiting forever.
    private func installTermHandler() {
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler { [weak self] in
            self?.log.info("SIGTERM: stopping session and exiting")
            self?.controller.stop(reason: .sigterm)
            exit(0)
        }
        source.resume()
        termSource = source
    }

    func applicationWillTerminate(_ notification: Notification) {
        controller.stop(reason: .quit)
    }

    private func tick() {
        controller.checkHealth(now: Date())
        watchSilence(now: Date())
        showIdleMeetingHint()
        recheckPendingBrowser()
        poll()
    }

    /// A browser keeps the mic for minutes after a call and plays sound outside calls, so neither
    /// system flag marks the end of a meeting reliably. Silence on both channels does: after
    /// `silencePromptAfter` seconds ask, after `silenceStopAfter` stop without asking.
    private func watchSilence(now: Date) {
        guard controller.current != nil else {
            stopPrompt.dismiss()
            stopPromptMutedUntilVoice = false
            return
        }
        let silent = controller.silentFor(now: now)
        if silent < Self.silencePromptAfter {
            stopPromptMutedUntilVoice = false
            if stopPrompt.isShowing { stopPrompt.dismiss() }   // someone spoke again
            return
        }
        if silent >= Self.silenceStopAfter {
            log.info("silence \(Int(silent))s: stopping recording")
            stopPrompt.dismiss()
            stopRecordingKeepingMeeting(reason: .silenceAuto)
            return
        }
        guard !stopPrompt.isShowing, !stopPromptMutedUntilVoice else { return }
        stopPrompt.show(PromptPanel.stop(minutes: Int(silent / 60)),
                        onPrimary: { [weak self] in self?.stopRecordingKeepingMeeting(reason: .silencePrompt) },
                        onSecondary: { [weak self] in self?.stopPromptMutedUntilVoice = true })
    }

    /// Stop without touching the detector: the app may still hold the mic, and re-offering to
    /// record the same meeting would be noise.
    private func stopRecordingKeepingMeeting(reason: StopReason) {
        controller.stop(reason: reason)
        declinedApp = activeApp
    }

    /// Meeting running, nothing being recorded (declined or the prompt timed out) — say so in the menu.
    private func showIdleMeetingHint() {
        guard activeApp != nil, controller.current == nil else { return }
        status.setHint("Встреча идёт, запись не ведётся")
    }

    /// `detector.poll` lists audio processes — a CoreAudio round trip that has no business
    /// blocking the main thread; only the resulting event is handled there.
    private func poll() {
        pollQueue.async { [weak self] in
            guard let self else { return }
            let processes = AudioProcesses.list()
            self.logMicHolders(processes)
            let event = self.detector.poll(now: Date(), processes: processes)
            let active = self.detector.activeApp
            DispatchQueue.main.async { self.handle(event, activeApp: active) }
        }
    }

    /// Who is holding the mic right now — the only way to learn which app to watch after a
    /// meeting goes undetected. Runs every 2 s, so it logs only when the set changes.
    /// `pollQueue` only.
    private func logMicHolders(_ processes: [AudioProcess]) {
        let holders = Set(processes
            .filter { $0.isRunningInput && !AudioProcesses.isOwn($0.bundleID) }
            .map { $0.bundleID.isEmpty ? "pid:\($0.pid)" : $0.bundleID })
        guard holders != lastMicHolders else { return }
        lastMicHolders = holders
        let list = holders.isEmpty ? "none" : "[" + holders.sorted().joined(separator: ", ") + "]"
        log.info("mic holders: \(list)")
    }

    private func handle(_ event: MeetingEvent?, activeApp: String?) {
        self.activeApp = activeApp
        guard let event else { return }
        switch event {
        case .started(let app):
            log.info("meeting detected in \(app)")
            if controller.current != nil || declinedApp == app { return }
            guard AppDelegate.browserApps.contains(app) else {
                showMeetingPrompt(app: app, info: nil, workContext: false)
                return
            }
            askBrowser(app: app)
        case .ended:
            log.info("meeting ended")
            declinedApp = nil
            pendingBrowserApp = nil
            pendingTicks = 0
            pendingIsPlaceholder = false
            placeholderSince = nil
            prompt.dismiss()
            controller.stop(reason: .meetingEnded)
            render(controller.state)   // drops the «встреча идёт» hint
        }
    }

    /// Chrome holds the mic for minutes after a call and any site can take it, so a browser on the
    /// mic is not yet a meeting: ask the browser what it has open, and only prompt on a call tab
    /// whose title is not still a placeholder.
    private func askBrowser(app: String) {
        pollQueue.async { [weak self] in
            let state = BrowserTabs.meetingTabState(forBrowser: app, hosts: AppDelegate.meetingHosts())
            // One verdict per detection: the log line, the prompt and the session that follows
            // all carry this one along instead of each asking the browser again — two answers to
            // «приватная ли» about the same meeting is exactly the bug worth avoiding.
            let work = SessionController.workContext(app: app)
            DispatchQueue.main.async { self?.handleBrowserState(state, app: app, workContext: work) }
        }
    }

    private func handleBrowserState(_ state: BrowserTabs.MeetingTabState, app: String, workContext: Bool) {
        guard controller.current == nil, declinedApp != app else { return }
        switch state {
        case .ready(let info):
            pendingBrowserApp = nil
            pendingTicks = 0
            pendingIsPlaceholder = false
            placeholderSince = nil
            // The name of a private call has no business in the log either.
            log.info(controller.wouldBePrivate(app: app, workContext: workContext)
                        ? "browser meeting tab found (приватное окно, название не пишем)"
                        : "browser meeting tab: '\(info.title)'")
            showMeetingPrompt(app: app, info: info, workContext: workContext)
        case .placeholder:
            let isNewApp = pendingBrowserApp != app
            if isNewApp || !pendingIsPlaceholder {
                log.info("browser meeting tab has placeholder title, waiting")
            }
            pendingBrowserApp = app
            pendingIsPlaceholder = true
            if placeholderSince == nil || isNewApp { placeholderSince = Date() }
            if let since = placeholderSince, Date().timeIntervalSince(since) >= AppDelegate.placeholderSafetyNet {
                log.info("placeholder title held \(Int(AppDelegate.placeholderSafetyNet))s+, prompting anyway")
                pendingBrowserApp = nil
                pendingTicks = 0
                pendingIsPlaceholder = false
                placeholderSince = nil
                showMeetingPrompt(app: app, info: MeetingInfo(title: "", participants: []), workContext: workContext)
            }
        case .none:
            if pendingBrowserApp != app {
                log.info("browser mic without a meeting tab, waiting")
            }
            pendingBrowserApp = app
            pendingIsPlaceholder = false
            placeholderSince = nil
        }
    }

    /// `info` is nil for a native meeting app, where the card has no title to lead with. A session
    /// the rules (or the menu override) call private gets the private card instead — and never the
    /// meeting name, even when the browser has already handed us one.
    private func showMeetingPrompt(app: String, info: MeetingInfo?, workContext: Bool) {
        let isPrivate = controller.wouldBePrivate(app: app, workContext: workContext)
        let spec = isPrivate
            ? PromptPanel.privateMeeting()
            : PromptPanel.meeting(appName: AppDelegate.promptName(for: app), title: info?.title)
        let decline: () -> Void = { [weak self] in
            self?.declinedApp = app
            self?.pendingBrowserApp = nil
        }
        prompt.show(spec,
                   onPrimary: { [weak self] in self?.controller.start(app: app, info: info, workContext: workContext) },
                   onSecondary: { [weak self] in
                       self?.log.info("meeting prompt: declined")
                       decline()
                   },
                   onTimeout: { [weak self] in
                       guard let self else { return }
                       // Asked again, not reused: the menu override or the clock may have moved
                       // while the card waited, and `start` will judge by the rules of this moment.
                       let privateNow = controller.wouldBePrivate(app: app, workContext: workContext)
                       switch UnansweredPrompt.action(isPrivate: privateNow) {
                       case .start:
                           log.info("meeting prompt: unanswered \(Int(spec.timeout))s, starting")
                           controller.start(app: app, info: info, workContext: workContext)
                       case .decline:
                           log.info("meeting prompt: unanswered \(Int(spec.timeout))s, private — not recording")
                           decline()
                       }
                   })
    }

    /// The call tab usually appears a beat after the mic does (and the Automation permission may
    /// only be granted mid-meeting), so keep re-asking while the browser holds the mic — faster
    /// while a placeholder title is the only thing blocking us.
    private func recheckPendingBrowser() {
        guard let app = pendingBrowserApp else {
            pendingTicks = 0
            return
        }
        guard controller.current == nil, declinedApp != app, activeApp == app, !prompt.isShowing else {
            pendingBrowserApp = nil
            pendingTicks = 0
            pendingIsPlaceholder = false
            placeholderSince = nil
            return
        }
        pendingTicks += 1
        let threshold = pendingIsPlaceholder ? AppDelegate.browserRecheckTicksPlaceholder : AppDelegate.browserRecheckTicks
        guard pendingTicks >= threshold else { return }
        pendingTicks = 0
        askBrowser(app: app)
    }

    /// Everything watched that is not a native meeting app: for those the tabs are the only
    /// source of a title, and the only way to tell a call from any other site.
    static let browserApps = MeetingDetector.defaultApps.filter {
        $0 != TalkInspector.bundleID && $0 != "us.zoom"   // leak-ok: bundle id Zoom, схема
    }

    /// A call in a browser tab (Толк web, Meet) is indistinguishable from any other tab from
    /// the outside, so the prompt names the browser rather than a meeting app.
    static func promptName(for app: String) -> String {
        switch app {
        case TalkInspector.bundleID: return "Толке"
        case "us.zoom": return "Zoom"   // leak-ok: bundle id Zoom, схема
        default: return "браузере"
        }
    }

    private func toggle() {
        if controller.current != nil {
            stopRecordingKeepingMeeting(reason: .manual)
        } else {
            prompt.dismiss()
            pendingBrowserApp = nil
            pendingTicks = 0
            pendingIsPlaceholder = false
            placeholderSince = nil
            let app = activeApp ?? "manual"
            guard AppDelegate.browserApps.contains(app) else {
                controller.start(app: app)
                return
            }
            // Inside a browser even «приватная ли» depends on the tabs — a Толк call in a private
            // slot is work — and AppleScript has no business on the main thread.
            pollQueue.async { [weak self] in
                let work = SessionController.workContext(app: app)
                DispatchQueue.main.async { self?.startManually(app: app, workContext: work) }
            }
        }
    }

    /// Second half of a manual start in a browser: the verdict belongs on the main thread (the
    /// menu override lives there), the title lookup does not. A private session takes no title
    /// from anywhere, so there is nothing to ask the browser about — and asking would put the
    /// meeting name in the AppleScript log.
    private func startManually(app: String, workContext: Bool) {
        guard !controller.wouldBePrivate(app: app, workContext: workContext) else {
            controller.start(app: app, workContext: workContext)
            return
        }
        // Started by hand while a browser holds the mic — the tab still knows the title.
        pollQueue.async { [weak self] in
            let info = BrowserTabs.meetingInfo(forBrowser: app, hosts: AppDelegate.meetingHosts())
            DispatchQueue.main.async { self?.controller.start(app: app, info: info, workContext: workContext) }
        }
    }

    private func openNote() { openNote(retrying: true) }

    /// The worker writes `note_path` a beat after the session starts, so the first click can
    /// legitimately find nothing — that is a hint, not a problem; one retry usually lands.
    private func openNote(retrying: Bool) {
        guard let dir = controller.current else { return }
        if openNote(path: dir.notePath()) {
            if !retrying { render(controller.state) }
            return
        }
        log.warn("note path not ready yet for \(dir.id)")
        guard retrying else {
            render(controller.state)
            return
        }
        status.setHint("Заметка ещё создаётся воркером")
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.openNote(retrying: false)
        }
    }

    /// A private note lives outside every vault, so `obsidian://open?path=` has nowhere to send it:
    /// hand the file to whatever opens .md instead.
    private func openNote(path: String?) -> Bool {
        guard controller.currentIsPrivate else { return Obsidian.open(path: path) }
        guard let path, !path.isEmpty, FileManager.default.fileExists(atPath: path) else { return false }
        return NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    private func render(_ state: SessionController.State) {
        status.setPrivateNext(controller.forcePrivateNext)   // the start that used it also spent it
        switch state {
        case .idle:
            status.setRecording(false, detail: nil)   // also clears a problem glyph or a hint
            floating.hide()
            recon?.stop()
            recon = nil
            talkObserver?.stop()
            talkObserver = nil
            axWatchersStarted = false
        case .recording(let dir):
            status.setRecording(true, detail: dir.id, private: controller.currentIsPrivate)
            floating.show(private: controller.currentIsPrivate, title: controller.currentTitle,
                          startedAt: controller.currentStartedAt, session: dir)
            startAXWatchers(dir)
        }
    }

    /// The two read-only watchers over the AX tree of the meeting app, started together because
    /// they need the same answer to «какое приложение и какой pid» — and that answer costs an
    /// AppleScript round trip to the browser.
    ///
    /// * `AXRecon` — the first minutes of the tree, for figuring out markings we do not know yet.
    /// * `TalkObserver` — the participant grid for the whole session, into `talk.jsonl`.
    ///
    /// Never for a private session: an AX dump — or a list of participants — of a private call is
    /// exactly the kind of thing the privacy rules exist to prevent. `render` can be called
    /// repeatedly for the same session, so `axWatchersStarted` is what keeps this to one attempt.
    private func startAXWatchers(_ dir: SessionDir) {
        guard !axWatchersStarted, !controller.currentIsPrivate, let app = controller.currentApp else { return }
        let recon = ReconConfig.load()
        let talk = TalkConfig.load()
        let wantsRecon = recon.enabled && recon.seconds > 0
        guard wantsRecon || talk.enabled else { return }
        axWatchersStarted = true
        let startedAt = controller.currentStartedAt ?? Date()
        // Asking the browser for its tabs is AppleScript, and looking up a pid walks the running
        // apps: neither belongs on the main thread.
        pollQueue.async { [weak self] in
            guard let self, let target = AppDelegate.reconTarget(app: app) else { return }
            guard let pid = AXDump.pidOf(bundlePrefix: target) else {
                self.log.warn("ax recon: no running app with prefix '\(target)'")
                return
            }
            DispatchQueue.main.async {
                // The session may already be over, or a different one may have started, by the
                // time the tabs answered.
                guard self.controller.current == dir else { return }
                if wantsRecon, self.recon == nil {
                    let watcher = AXRecon(pid: pid, sessionID: dir.id, startedAt: startedAt,
                                          seconds: recon.seconds)
                    self.recon = watcher
                    watcher.start()
                }
                if talk.enabled, self.talkObserver == nil {
                    let observer = TalkObserver(pid: pid, sessionDir: dir, startedAt: startedAt)
                    self.talkObserver = observer
                    observer.start()
                }
            }
        }
    }

    /// Reads both AX blocks once at launch — which also writes them into the config the first time,
    /// so there is something to switch off without waiting for a meeting. On `pollQueue`, one after
    /// the other: it is file I/O into the same file, and the blocks may have to be spliced in.
    private func announceReconConfig() {
        pollQueue.async { [weak self] in
            let recon = ReconConfig.load()
            self?.log.info("ax recon config: enabled \(recon.enabled), \(Int(recon.seconds))s")
            let talk = TalkConfig.load()
            self?.log.info("talk observer config: enabled \(talk.enabled)")
            if PrivacyRules.load().workHosts.isEmpty {
                self?.log.warn("privacy: workHosts пуст — звонки в браузере на корпоративном хосте не распознаются; задайте хосты в «Настройки…»")
            }
        }
    }

    /// Hosts that count as a call tab: the built-in services plus the corporate host from the config.
    static func meetingHosts() -> [String] {
        BrowserTabs.defaultMeetingHosts + PrivacyRules.load().workHosts
    }

    /// Which app's tree is worth sampling: the Толк desktop app, or a browser that has a Толк call
    /// open. Everything else (a Zoom call, a browser on Meet) is not what this reconnaissance is
    /// about, and dumping it would only cost CPU. Off the main thread — it may ask the browser.
    static func reconTarget(app: String) -> String? {
        if app.hasPrefix(TalkInspector.bundleID) { return app }
        let hosts = PrivacyRules.load().workHosts
        // Ручной старт («manual») не знает, где встреча: обходим браузеры и берём тот,
        // у которого открыта вкладка со звонком в Толке.
        let candidates = browserApps.contains(app) ? [app] : (app == "manual" ? browserApps : [])
        return candidates.first { BrowserTabs.hasWorkMeeting(forBrowser: $0, hosts: hosts) }
    }
}
