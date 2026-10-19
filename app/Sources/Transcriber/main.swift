import ApplicationServices
import AppKit
import Foundation
import TranscriberCore

/// `Transcriber --record-test 8` records 8 seconds into a session and prints peak levels
/// per input to the log; used to verify capture + permissions without a meeting.
if let i = CommandLine.arguments.firstIndex(of: "--record-test") {
    let seconds = Double(CommandLine.arguments.dropFirst(i + 1).first ?? "8") ?? 8
    let log = Log.shared
    let store = SessionStore(root: SessionStore.defaultRoot.appendingPathComponent("record-test"))
    let dir = try! store.create(app: "manual", title: "record-test", participants: [])
    let mic = try! PcmWriter(url: dir.micURL)
    let sys = try! PcmWriter(url: dir.sysURL)
    let cap = TapCapture()
    var micMixer: Downmixer?
    var sysMixer: Downmixer?
    var micPeak: Float = 0
    var sysPeak: Float = 0
    try! cap.start { chunk in
        guard chunk.frames > 0 else { return }
        // Each mixer is built on its own: one failing must not leave the other permanently nil.
        if micMixer == nil {
            micMixer = try? Downmixer(inputRate: cap.sampleRate, inputChannels: max(chunk.mic.count / chunk.frames, 1))
        }
        if sysMixer == nil {
            sysMixer = try? Downmixer(inputRate: cap.sampleRate, inputChannels: max(cap.sysChannels, 1))
        }
        guard let mm = micMixer, let sm = sysMixer else {
            log.warn("record-test: downmixer unavailable (mic \(micMixer != nil), sys \(sysMixer != nil)), chunk skipped")
            return
        }
        micPeak = max(micPeak, chunk.mic.map(abs).max() ?? 0)
        sysPeak = max(sysPeak, chunk.sys.map(abs).max() ?? 0)
        mic.write(mm.process(interleaved: chunk.mic, frames: chunk.frames))
        sys.write(sm.process(interleaved: chunk.sys, frames: chunk.frames))
    }
    let say = Process()
    say.executableURL = URL(fileURLWithPath: "/usr/bin/say")
    say.arguments = ["-v", "Milena", "Проверка захвата системного звука транскрибатором, раз, два, три"]
    try? say.run()
    Thread.sleep(forTimeInterval: seconds)
    cap.stop()
    mic.close()
    sys.close()
    try? dir.requestStop()
    log.info("record-test: \(dir.url.path) mic peak \(micPeak) sys peak \(sysPeak) mic samples \(mic.samplesWritten) sys samples \(sys.samplesWritten)")
    print("record-test done: \(dir.url.path) mic peak \(micPeak) sys peak \(sysPeak)")
    exit(0)
}

/// `Transcriber --ax-dump <bundle-prefix> [seconds] [interval]` dumps the Accessibility tree of a
/// running app every `interval` seconds for `seconds`, writing a file per *changed* snapshot to
/// ~/Library/Logs/transcriber-ax. Reconnaissance only: nothing is clicked, nothing is parsed.
/// Needs the Accessibility grant, so run the installed bundle:
/// `open -n -a ~/Applications/Transcriber.app --args --ax-dump kontur.talk 60 2`.
if let i = CommandLine.arguments.firstIndex(of: "--ax-dump") {
    let rest = Array(CommandLine.arguments.dropFirst(i + 1))
    let prefix = rest.first ?? TalkInspector.bundleID
    let seconds = Double(rest.dropFirst().first ?? "60") ?? 60
    let interval = max(Double(rest.dropFirst(2).first ?? "2") ?? 2, 0.2)
    let log = Log.shared
    guard TalkInspector.ensureAccessibility() else {
        print("ax-dump: нет разрешения Accessibility")
        log.warn("ax-dump: accessibility not granted")
        exit(1)
    }
    guard let pid = AXDump.pidOf(bundlePrefix: prefix) else {
        print("ax-dump: приложение с префиксом '\(prefix)' не запущено")
        log.warn("ax-dump: no running app with prefix '\(prefix)'")
        exit(1)
    }
    // Chrome and Electron build the web-content tree only after the flags are set: give the app a
    // beat to do it, otherwise the first snapshot is a stub of a dozen nodes.
    AXDump.enableEnhancedAccessibility(AXUIElementCreateApplication(pid))
    Thread.sleep(forTimeInterval: 1)
    let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/transcriber-ax")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let stamp = DateFormatter()
    stamp.dateFormat = "yyyy-MM-dd-HHmmss"
    var previous: String?
    var written = 0
    let deadline = Date().addingTimeInterval(seconds)
    repeat {
        let text = AXDump.dump(pid: pid)
        if text != previous {
            previous = text
            let url = dir.appendingPathComponent("\(stamp.string(from: Date()))-\(prefix).txt")
            try? text.write(to: url, atomically: true, encoding: .utf8)
            written += 1
            log.info("ax-dump: \(url.lastPathComponent), \(text.split(separator: "\n").count) строк")
        }
        Thread.sleep(forTimeInterval: interval)
    } while Date() < deadline
    print("ax-dump: \(prefix) pid \(pid), снимков записано \(written) в \(dir.path)")
    log.info("ax-dump: \(prefix) pid \(pid), \(written) snapshots in \(dir.path)")
    exit(0)
}

/// `Transcriber --recon-test <bundle-prefix> [seconds]` runs the very reconnaissance a recording
/// starts (`AXRecon`), writing into ~/Library/Logs/transcriber-ax/recon-test-<stamp>/ — the way to
/// check the change log and the summary against a real Толк call without waiting for a session.
/// Needs the Accessibility grant, so run the installed bundle.
if let i = CommandLine.arguments.firstIndex(of: "--recon-test") {
    let rest = Array(CommandLine.arguments.dropFirst(i + 1))
    let prefix = rest.first ?? TalkInspector.bundleID
    let seconds = Double(rest.dropFirst().first ?? "20") ?? 20
    guard TalkInspector.ensureAccessibility() else {
        print("recon-test: нет разрешения Accessibility")
        exit(1)
    }
    guard let pid = AXDump.pidOf(bundlePrefix: prefix) else {
        print("recon-test: приложение с префиксом '\(prefix)' не запущено")
        exit(1)
    }
    let stamp = DateFormatter()
    stamp.dateFormat = "yyyy-MM-dd-HHmmss"
    let recon = AXRecon(pid: pid, sessionID: "recon-test-\(stamp.string(from: Date()))", seconds: seconds)
    recon.start()
    // The sampler owns its own queue; this thread only has to outlive the run.
    Thread.sleep(forTimeInterval: seconds + AXRecon.firstSampleDelay + 1)
    recon.stop()
    Thread.sleep(forTimeInterval: 0.5)
    print("recon-test: \(prefix) pid \(pid) → \(recon.dir.path)")
    exit(0)
}

/// `Transcriber --talk-test <bundle-prefix> [seconds]` runs the very grid watcher a recording
/// starts (`TalkObserver`) against a running Толк — desktop app or the browser holding the call —
/// writing `talk.jsonl` into a throwaway session and printing the last snapshot it read.
/// Needs the Accessibility grant, so run the installed bundle:
/// `open -n -a ~/Applications/Transcriber.app --args --talk-test com.google.Chrome 20`.
if let i = CommandLine.arguments.firstIndex(of: "--talk-test") {
    let rest = Array(CommandLine.arguments.dropFirst(i + 1))
    let prefix = rest.first ?? TalkInspector.bundleID
    let seconds = Double(rest.dropFirst().first ?? "20") ?? 20
    guard TalkInspector.ensureAccessibility() else {
        print("talk-test: нет разрешения Accessibility")
        exit(1)
    }
    guard let pid = AXDump.pidOf(bundlePrefix: prefix) else {
        print("talk-test: приложение с префиксом '\(prefix)' не запущено")
        exit(1)
    }
    let store = SessionStore(root: FileManager.default.temporaryDirectory
        .appendingPathComponent("transcriber-talk-test"))
    let dir = try! store.create(app: prefix, title: "talk-test", participants: [])
    let observer = TalkObserver(pid: pid, sessionDir: dir)
    observer.start()
    // The observer owns its own queue; this thread only has to outlive the run.
    Thread.sleep(forTimeInterval: seconds + TalkObserver.firstSampleDelay + 0.5)
    observer.stop()
    Thread.sleep(forTimeInterval: 0.5)
    let lines = (try? String(contentsOf: observer.url, encoding: .utf8))?
        .split(separator: "\n").map(String.init) ?? []
    print("talk-test: \(prefix) pid \(pid) → \(observer.url.path)")
    print("строк в talk.jsonl: \(lines.count)")
    if let snapshot = observer.lastSnapshot {
        print("последний снимок: участников \(snapshot.participants.count), "
              + "говорит '\(snapshot.activeName ?? "—")'")
        for p in snapshot.participants {
            print("  \(p.active ? "▶" : " ") \(p.micOn ? "🎤" : "🔇") \(p.name)")
        }
    } else {
        print("последний снимок: группы участников в дереве не было — звонок открыт не в этой "
              + "вкладке или встречи нет")
    }
    if let last = lines.last { print("последняя строка: \(last)") }
    exit(0)
}

/// `Transcriber --devices` prints the current default input and output devices and exits — the
/// quick way to check what macOS thinks it is recording from and playing to, without a session.
if CommandLine.arguments.contains("--devices") {
    let input = AudioDevices.defaultInput()
    let output = AudioDevices.defaultOutput()
    print("вход:  \(AudioDevices.name(of: input)) [\(input?.uid ?? "—")]")
    print("выход: \(AudioDevices.name(of: output)) [\(output?.uid ?? "—")]")
    exit(0)
}

/// `Transcriber --tabs-test [bundleID]` lists the browser's tabs and shows which one it takes for
/// a meeting; the first run may raise the Automation permission dialog.
if let i = CommandLine.arguments.firstIndex(of: "--tabs-test") {
    let bundleID = CommandLine.arguments.dropFirst(i + 1).first ?? "com.google.Chrome"
    let tabs = BrowserTabs.tabs(ofBrowser: String(bundleID))
    print("\(bundleID): \(tabs.count) вкладок")
    if let meeting = BrowserTabs.pickMeeting(tabs, hosts: AppDelegate.meetingHosts()) {
        print("встреча: \(meeting.title)")
        print("url: \(meeting.url)")
        print("название: '\(BrowserTabs.cleanTitle(meeting.title))'")
    } else {
        print("встреча: не найдена")
    }
    exit(0)
}

/// `Transcriber --prompt-test [seconds]` shows the meeting prompt for a visual check, then exits.
if let i = CommandLine.arguments.firstIndex(of: "--prompt-test") {
    let seconds = Double(CommandLine.arguments.dropFirst(i + 1).first ?? "6") ?? 6
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let prompt = PromptPanel()
    DispatchQueue.main.async {
        if CommandLine.arguments.contains("stop") {
            prompt.show(PromptPanel.stop(minutes: 3), onPrimary: { print("stop") }, onSecondary: { print("continue") })
        } else if CommandLine.arguments.contains("private") {
            prompt.show(PromptPanel.privateMeeting(), onPrimary: { print("record") }, onSecondary: { print("skip") })
        } else {
            // `--prompt-test title «…»` checks the browser card, which leads with the meeting name.
            let j = CommandLine.arguments.firstIndex(of: "title")
            let title: String? = j.flatMap { CommandLine.arguments.dropFirst($0 + 1).first }
            prompt.show(PromptPanel.meeting(appName: "браузере", title: title),
                        onPrimary: { print("record") }, onSecondary: { print("skip") })
        }
    }
    if let j = CommandLine.arguments.firstIndex(of: "--prompt-png"), let path = CommandLine.arguments.dropFirst(j + 1).first {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { prompt.snapshot(to: URL(fileURLWithPath: path)) }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { prompt.dismiss(); exit(0) }
    app.run()
}

/// `Transcriber --transcript-test <session dir> [seconds] [--transcript-png <path>]` opens the live
/// transcript window on an existing session dir (a fake one is enough: `state.json` with `raw_path`)
/// for a visual check, then exits.
if let i = CommandLine.arguments.firstIndex(of: "--transcript-test"),
   let path = CommandLine.arguments.dropFirst(i + 1).first {
    let seconds = Double(CommandLine.arguments.dropFirst(i + 2).first ?? "8") ?? 8
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let window = TranscriptWindow()
    window.onOpenNote = { print("open note") }
    window.onStop = { print("stop") }
    window.onHide = { print("hide") }
    DispatchQueue.main.async {
        window.show(session: SessionDir(url: URL(fileURLWithPath: path)), title: "Тестовая встреча",
                    startedAt: Date().addingTimeInterval(-754), near: nil)
    }
    if let j = CommandLine.arguments.firstIndex(of: "--transcript-png"), let png = CommandLine.arguments.dropFirst(j + 1).first {
        DispatchQueue.main.asyncAfter(deadline: .now() + max(1, seconds - 0.5)) { window.snapshot(to: URL(fileURLWithPath: png)) }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { window.close(); exit(0) }
    app.run()
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
