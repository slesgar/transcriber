import AppKit
import TranscriberCore

/// Window of the running meeting, opened by a click on the floating button: the text recognised so
/// far (`LiveTranscriptFollower`), the recording actions, and a question field for the owner's own
/// agent. An answer slides up as a sheet over ~70% of the transcript; every question of the meeting
/// is one conversation (`MeetingChat`), kept in the session's `qa.jsonl`.
///
/// Non-activating: the meeting app keeps focus, yet the panel can still become key, so the text
/// scrolls, selects and copies and the field takes typing. Kept out of screen sharing — a live
/// transcript on a shared screen is exactly the leak a private recording exists to avoid.
final class TranscriptWindow: NSObject, NSWindowDelegate {
    var onOpenNote: (() -> Void)?
    var onStop: (() -> Void)?
    var onHide: (() -> Void)?

    private var panel: NSPanel?
    private var textView: NSTextView?
    private let pill = NSTextField(labelWithString: "")
    private let pillBox = NSView()
    private let updatedLabel = NSTextField(labelWithString: "")
    private var timer: Timer?
    private var follower: LiveTranscriptFollower?
    private var startedAt: Date?
    private var isPrivate = false
    /// The body on screen; nil — the placeholder (or nothing yet).
    private var shownBody: String?
    private var changedAt: Date?
    private var speakerColors: [String: NSColor] = [:]

    // answers sheet
    private let sheet = NSBox()
    private let sheetQuestion = NSTextField(labelWithString: "")
    private let sheetMeta = NSTextField(labelWithString: "")
    private var answersView: NSTextView?
    private var sheetHeight: NSLayoutConstraint?
    private let reopen = NSButton()
    private var sheetOpen = false

    // composer
    private let input = NSTextField()
    private let contextSwitch = NSSwitch()
    private let contextLabel = NSTextField(labelWithString: "проекты")
    private let send = NSButton()
    private let spinner = NSProgressIndicator()
    private let composer = NSStackView()
    private let setupAgent = NSButton(title: "Подключить агента…", target: nil, action: nil)
    private let privateNote = NSTextField(labelWithString: "Приватная запись — вопросы агенту выключены")

    private var qas: [MeetingQA] = []
    private var pending: MeetingQA?
    private var running: AgentRun?
    /// The question in flight: a completion that is not for it (the window was reset and reopened
    /// meanwhile) only lands in the log, never in the window.
    private var askToken: UUID?
    private var meetingTitle: String?
    private var renderedCount = -1

    static let waitingText = "Текст появится, когда воркер распознает первый блок — обычно через минуту-две после начала речи."
    static let tick: TimeInterval = 1
    static let contextDefaultsKey = "agentWithContext"

    var isVisible: Bool { panel?.isVisible ?? false }
    var session: SessionDir? { follower?.session }

    func show(session: SessionDir, title: String?, startedAt: Date?, near anchor: NSRect?, private priv: Bool = false) {
        if follower?.session != session { forget() }
        if follower == nil {
            follower = LiveTranscriptFollower(session: session)
            qas = MeetingChat.load(session)
        }
        isPrivate = priv || session.isPrivate()   // either source saying «private» is enough
        self.startedAt = startedAt
        meetingTitle = title?.isEmpty == false ? title : nil
        let p = panel ?? makePanel()
        p.title = meetingTitle ?? "Транскрипт"
        let opening = !p.isVisible
        if opening { place(p, near: anchor) }
        refreshComposer()
        renderAnswers()
        refresh()
        if opening { textView?.scrollToEndOfDocument(nil) }   // a fresh look starts at the latest line
        p.orderFrontRegardless()
        p.makeKey()
        if timer == nil {
            let t = Timer(timeInterval: Self.tick, repeats: true) { [weak self] _ in self?.refresh() }
            RunLoop.main.add(t, forMode: .common)
            timer = t
        }
    }

    /// Closing only stops the ticking; the text, the chat and the follower stay, so reopening is instant.
    func close() {
        panel?.orderOut(nil)
        stopTicking()
    }

    /// Recording is over: forget the session so a later click cannot show a stale meeting. A
    /// question still running finishes in the background and lands in its own session's log.
    func reset() {
        close()
        forget()
    }

    func windowWillClose(_ notification: Notification) { stopTicking() }

    /// The owner may have just set an agent up in «Настройки…»: the field follows on return.
    func windowDidBecomeKey(_ notification: Notification) { refreshComposer() }

    private func stopTicking() {
        timer?.invalidate()
        timer = nil
    }

    private func forget() {
        follower = nil
        startedAt = nil
        shownBody = nil
        changedAt = nil
        speakerColors = [:]
        qas = []
        pending = nil
        running = nil
        askToken = nil
        renderedCount = -1
        textView?.string = ""
        setSheet(open: false)
    }

    // MARK: - Transcript

    private func refresh() {
        pill.stringValue = (isPrivate ? "● Приватная · " : "● Запись · ") + FloatingButton.elapsed(since: startedAt)
        let tint: NSColor = isPrivate ? .systemPurple : .systemRed
        pill.textColor = tint
        pillBox.layer?.backgroundColor = tint.withAlphaComponent(0.13).cgColor
        updatedLabel.stringValue = Self.updated(changedAt)
        if let pending {   // only the counter moves; the sheet text stays put for reading and selecting
            sheetMeta.stringValue = Self.meta(of: pending, pending: true) + " · думаю… \(Int(Date().timeIntervalSince(pending.askedAt))) с"
        }
        guard let textView, let now = follower?.poll() else { return }
        switch now {
        case .waiting:
            guard shownBody != nil || textView.string.isEmpty else { return }   // keep what was shown
            shownBody = nil
            textView.textStorage?.setAttributedString(NSAttributedString(string: Self.waitingText, attributes: [
                .font: Self.font, .foregroundColor: NSColor.secondaryLabelColor]))
        case .text(let body):
            show(body, in: textView)
        }
    }

    static func updated(_ at: Date?) -> String {
        guard let at else { return "" }
        let s = Int(Date().timeIntervalSince(at))
        if s < 10 { return "текст обновлён только что" }
        if s < 60 { return "текст обновлён \(s) с назад" }
        return "текст обновлён \(s / 60) мин назад"
    }

    /// New replies are appended, so a selection in progress and the scroll position survive the
    /// worker's rewrite; only a real rewrite of earlier text replaces everything.
    private func show(_ body: String, in textView: NSTextView) {
        guard body != shownBody else { return }
        changedAt = Date()
        let atBottom = Self.isScrolledToBottom(textView)
        if let old = shownBody, let tail = LiveTranscript.appendix(from: old, to: body),
           let storage = textView.textStorage {
            storage.append(styled(tail))
        } else {
            let origin = textView.enclosingScrollView?.contentView.bounds.origin
            textView.textStorage?.setAttributedString(styled(body))
            if !atBottom, let origin, let clip = textView.enclosingScrollView?.contentView {
                clip.scroll(to: origin)
                textView.enclosingScrollView?.reflectScrolledClipView(clip)
            }
        }
        shownBody = body
        if atBottom { textView.scrollToEndOfDocument(nil) }
    }

    private static let font = NSFont.systemFont(ofSize: 13)
    private static let palette: [NSColor] = [.systemPurple, .systemTeal, .systemOrange, .systemPink,
                                             .systemIndigo, .systemBrown, .systemGreen]

    /// `Name: text` lines get a coloured bold name (the owner's «Me» in the accent colour, others in
    /// order of appearance); `— 00:05 —` markers become quiet centred dividers.
    private func styled(_ text: String) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let body = NSMutableParagraphStyle()
        body.paragraphSpacing = 2
        body.lineSpacing = 1.5
        let marker = NSMutableParagraphStyle()
        marker.alignment = .center
        let lines = text.components(separatedBy: "\n")
        for (i, line) in lines.enumerated() {
            let nl = i < lines.count - 1 ? "\n" : ""
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("—"), trimmed.hasSuffix("—"), trimmed.count > 2 {
                out.append(NSAttributedString(string: line + nl, attributes: [
                    .font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.tertiaryLabelColor, .paragraphStyle: marker]))
                continue
            }
            if let colon = line.range(of: ": "), line.distance(from: line.startIndex, to: colon.lowerBound) <= 40 {
                let name = String(line[..<colon.lowerBound])
                out.append(NSAttributedString(string: name, attributes: [
                    .font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: color(for: name), .paragraphStyle: body]))
                out.append(NSAttributedString(string: "  " + line[colon.upperBound...] + nl, attributes: [
                    .font: Self.font, .foregroundColor: NSColor.labelColor, .paragraphStyle: body]))
                continue
            }
            out.append(NSAttributedString(string: line + nl, attributes: [
                .font: Self.font, .foregroundColor: NSColor.labelColor, .paragraphStyle: body]))
        }
        return out
    }

    private func color(for speaker: String) -> NSColor {
        if speaker == "Me" { return .controlAccentColor }
        if let c = speakerColors[speaker] { return c }
        let c = Self.palette[speakerColors.count % Self.palette.count]
        speakerColors[speaker] = c
        return c
    }

    /// Follow new lines only when the reader is already at the end: scrolled up = reading, leave it.
    private static func isScrolledToBottom(_ textView: NSTextView) -> Bool {
        guard let scroll = textView.enclosingScrollView else { return true }
        let visible = scroll.contentView.documentVisibleRect
        return visible.maxY >= textView.bounds.height - 24
    }

    // MARK: - Asking the agent

    private func refreshComposer() {
        let config = AgentConfig.load()
        let hasAgent = config.isConfigured && !isPrivate
        input.isHidden = !hasAgent
        send.isHidden = !hasAgent
        let canContext = hasAgent && config.contextURL != nil
        contextSwitch.isHidden = !canContext
        contextLabel.isHidden = !canContext
        contextSwitch.state = UserDefaults.standard.bool(forKey: Self.contextDefaultsKey) ? .on : .off
        setupAgent.isHidden = isPrivate || config.isConfigured
        privateNote.isHidden = !isPrivate
        input.placeholderString = "Спросить \(AgentPreset.displayName(for: config.command)) про встречу…"
        setRunning(running != nil)
    }

    /// Return in the field asks; it never stops a question already running (the owner is typing the next one).
    @objc private func askFromField() {
        if running != nil { NSSound.beep(); return }
        ask()
    }

    /// The round button: ask when idle, stop the agent while it thinks.
    @objc private func askOrStop() {
        if let running {
            running.cancel()
            return
        }
        ask()
    }

    private func ask() {
        let question = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty, let session else { return }
        let config = AgentConfig.load()
        guard config.isConfigured, !isPrivate else { refreshComposer(); return }
        let withContext = !contextSwitch.isHidden && contextSwitch.state == .on
        var transcript = ""
        if case .text(let body) = LiveTranscript.read(session) { transcript = body }
        let prompt = MeetingChat.prompt(question: question, transcript: transcript, history: qas,
                                        title: meetingTitle, withContext: withContext)
        let cwd = withContext ? config.contextURL : Self.emptyDir()
        let asked = MeetingQA(askedAt: Date(), question: question,
                              agent: AgentPreset.displayName(for: config.command), withContext: withContext)
        pending = asked
        let token = UUID()
        askToken = token
        input.stringValue = ""
        setSheet(open: true)
        renderAnswers()
        running = AgentRunner.run(command: config.command, prompt: prompt, cwd: cwd,
                                  timeout: TimeInterval(config.timeoutS)) { [weak self] result in
            var qa = asked
            qa.seconds = Int(Date().timeIntervalSince(asked.askedAt).rounded())
            switch result {
            case .answer(let text): qa.answer = text
            case .failed(let code, let message):
                qa.error = "Агент не ответил (код \(code))" + (message.isEmpty ? "" : ": \(message)")
            case .timedOut: qa.error = "Агент не ответил за \(qa.seconds ?? 0) с"
            case .cancelled: qa.error = "Остановлено"
            case .notConfigured: qa.error = "Агент не настроен"
            }
            // the log belongs to the meeting the question was asked in, even if the window moved on
            do { try MeetingChat.append(qa, to: session) } catch {
                Log.shared.warn("agent: answer not saved to \(session.qaURL.path): \(error)")
            }
            guard let self, self.askToken == token else { return }
            self.askToken = nil
            self.qas.append(qa)
            self.pending = nil
            self.running = nil
            self.setRunning(false)
            self.renderAnswers()
        }
        setRunning(true)
    }

    @objc private func toggleContext() {
        UserDefaults.standard.set(contextSwitch.state == .on, forKey: Self.contextDefaultsKey)
    }

    @objc private func openAgentSettings() { SettingsWindow.shared.showAgent() }

    private func setRunning(_ on: Bool) {
        send.image = NSImage(systemSymbolName: on ? "stop.circle.fill" : "arrow.up.circle.fill",
                             accessibilityDescription: on ? "Остановить агента" : "Спросить")
        send.toolTip = on ? "Остановить агента" : "Спросить (↩)"
        if on { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        spinner.isHidden = !on
    }

    /// An agent without project context starts in an empty folder: it sees the meeting and nothing else.
    private static func emptyDir() -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("transcriber-agent", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    // MARK: - Answers sheet

    private func renderAnswers() {
        guard let answersView else { return }
        let all = qas + (pending.map { [$0] } ?? [])
        reopen.isHidden = sheetOpen || all.isEmpty
        reopen.title = "Ответы · \(all.count)"
        guard let last = all.last else { return }
        sheetQuestion.stringValue = pending != nil ? "Агент думает…" : (all.count == 1 ? "Ответ агента" : "Ответы агента · \(all.count)")
        sheetMeta.stringValue = Self.meta(of: last, pending: pending != nil)
        let out = NSMutableAttributedString()
        let gap = NSMutableParagraphStyle()
        gap.paragraphSpacingBefore = 14
        for (i, qa) in all.enumerated() {
            let isPending = pending != nil && i == all.count - 1
            do {
                out.append(NSAttributedString(string: qa.question + "\n", attributes: [
                    .font: NSFont.systemFont(ofSize: 12, weight: .semibold), .foregroundColor: NSColor.secondaryLabelColor,
                    .paragraphStyle: i == 0 ? NSParagraphStyle.default : gap]))
            }
            if isPending {
                out.append(NSAttributedString(string: "…\n", attributes: [
                    .font: Self.font, .foregroundColor: NSColor.tertiaryLabelColor]))
            } else if let answer = qa.answer {
                out.append(Self.markdown(answer))
                out.append(NSAttributedString(string: "\n"))
            } else {
                out.append(NSAttributedString(string: (qa.error ?? "Нет ответа") + "\n", attributes: [
                    .font: Self.font, .foregroundColor: NSColor.systemRed]))
            }
            if all.count > 1 && !isPending && i < all.count - 1 {
                out.append(NSAttributedString(string: Self.meta(of: qa, pending: false) + "\n", attributes: [
                    .font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.tertiaryLabelColor]))
            }
        }
        answersView.textStorage?.setAttributedString(out)
        if all.count != renderedCount || pending == nil {   // new question or its answer: show the end
            answersView.scrollToEndOfDocument(nil)
        }
        renderedCount = all.count
    }

    private static func meta(of qa: MeetingQA, pending: Bool) -> String {
        var parts = [qa.agent]
        if qa.withContext { parts.append("с проектами") }
        if !pending, let s = qa.seconds { parts.append("\(s) с") }
        return parts.joined(separator: " · ")
    }

    /// The agent's markdown (bold, lists, code) shown as rich text; anything unparsable stays plain.
    static func markdown(_ text: String) -> NSAttributedString {
        // blank lines between paragraphs become paragraph spacing, not empty lines
        let text = text.replacingOccurrences(of: "\n[ \t]*\n+", with: "\n", options: .regularExpression)
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        let base: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.labelColor]
        guard let parsed = try? NSMutableAttributedString(markdown: text, options: options) else {
            return NSAttributedString(string: text, attributes: base)
        }
        let full = NSRange(location: 0, length: parsed.length)
        parsed.addAttribute(.foregroundColor, value: NSColor.labelColor, range: full)
        parsed.removeAttribute(.link, range: full)   // a link from the transcript is not something to click
        let para = NSMutableParagraphStyle()
        para.lineSpacing = 2
        para.paragraphSpacing = 4
        parsed.addAttribute(.paragraphStyle, value: para, range: full)
        parsed.addAttribute(.font, value: font, range: full)
        // the parser marks emphasis with presentation intents, not fonts
        parsed.enumerateAttribute(.inlinePresentationIntent, in: full) { value, range, _ in
            guard let raw = (value as? NSNumber)?.uintValue else { return }
            let intent = InlinePresentationIntent(rawValue: raw)
            var f = font
            if intent.contains(.stronglyEmphasized) { f = .systemFont(ofSize: 13, weight: .semibold) }
            if intent.contains(.code) { f = .monospacedSystemFont(ofSize: 12, weight: .regular) }
            if intent.contains(.emphasized) { f = NSFontManager.shared.convert(f, toHaveTrait: .italicFontMask) }
            parsed.addAttribute(.font, value: f, range: range)
        }
        return parsed
    }

    private func setSheet(open: Bool) {
        sheetOpen = open
        sheet.isHidden = !open
        reopen.isHidden = open || (qas.isEmpty && pending == nil)
    }

    @objc private func collapseSheet() { setSheet(open: false) }
    @objc private func reopenSheet() { setSheet(open: true); renderAnswers() }

    @objc private func copyAnswer() {
        guard let text = (pending == nil ? qas.last?.answer : nil), !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    // MARK: - Building the panel

    private func place(_ p: NSPanel, near anchor: NSRect?) {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let f = screen.visibleFrame
        let size = p.frame.size
        var origin = NSPoint(x: f.maxX - size.width - 60, y: f.midY - size.height / 2)
        if let anchor {   // to the left of the button, vertically centred on it
            origin = NSPoint(x: anchor.minX - size.width - 8, y: anchor.midY - size.height / 2)
        }
        origin.x = min(max(origin.x, f.minX), f.maxX - size.width)
        origin.y = min(max(origin.y, f.minY), f.maxY - size.height)
        p.setFrameOrigin(origin)
    }

    private func makePanel() -> NSPanel {
        let p = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 640),
                         styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel, .fullSizeContentView],
                         backing: .buffered, defer: false)
        p.level = .floating
        p.isFloatingPanel = true
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        p.sharingType = .none
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.minSize = NSSize(width: 400, height: 420)
        p.titlebarAppearsTransparent = true
        p.delegate = self

        let root = NSVisualEffectView()
        root.material = .popover
        root.blendingMode = .behindWindow
        root.state = .active

        let header = makeHeader()
        let stage = makeStage()
        let line = NSBox()
        line.boxType = .separator
        let bottom = makeComposer()
        for v in [header, stage, line, bottom] {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
        }
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: root.safeAreaLayoutGuide.topAnchor, constant: 6),
            header.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 12),
            header.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -10),
            stage.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 6),
            stage.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stage.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            line.topAnchor.constraint(equalTo: stage.bottomAnchor),
            line.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            line.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            bottom.topAnchor.constraint(equalTo: line.bottomAnchor, constant: 10),
            bottom.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 12),
            bottom.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -12),
            bottom.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -12),
        ])
        p.contentView = root
        panel = p
        return p
    }

    private func makeHeader() -> NSView {
        pill.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        pill.textColor = .systemRed
        pillBox.wantsLayer = true
        pillBox.layer?.cornerRadius = 10
        pillBox.layer?.backgroundColor = NSColor.systemRed.withAlphaComponent(0.13).cgColor
        pill.translatesAutoresizingMaskIntoConstraints = false
        pillBox.addSubview(pill)
        NSLayoutConstraint.activate([
            pill.leadingAnchor.constraint(equalTo: pillBox.leadingAnchor, constant: 9),
            pill.trailingAnchor.constraint(equalTo: pillBox.trailingAnchor, constant: -9),
            pill.topAnchor.constraint(equalTo: pillBox.topAnchor, constant: 3),
            pill.bottomAnchor.constraint(equalTo: pillBox.bottomAnchor, constant: -3),
        ])
        updatedLabel.font = .systemFont(ofSize: 11)
        updatedLabel.textColor = .tertiaryLabelColor
        updatedLabel.lineBreakMode = .byTruncatingTail
        updatedLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let note = iconButton("note.text", "Открыть заметку встречи", #selector(openNote))
        let hide = iconButton("eye.slash", "Скрыть кружок до конца записи", #selector(hideButton))
        let stop = iconButton("stop.circle", "Остановить запись", #selector(stopRecording))
        stop.contentTintColor = .systemRed
        let bar = NSStackView(views: [pillBox, updatedLabel, spacer, note, hide, stop])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.alignment = .centerY
        return bar
    }

    /// The transcript with the answers sheet laid over its lower part.
    private func makeStage() -> NSView {
        let stage = NSView()
        stage.wantsLayer = true
        stage.layer?.masksToBounds = true

        let scroll = NSTextView.scrollableTextView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder
        scroll.drawsBackground = false
        guard let text = scroll.documentView as? NSTextView else { fatalError("scrollableTextView has a text view") }
        text.isEditable = false
        text.isSelectable = true
        text.isRichText = true
        text.drawsBackground = false
        text.font = Self.font
        text.textContainerInset = NSSize(width: 12, height: 8)
        textView = text
        scroll.automaticallyAdjustsContentInsets = false
        scroll.contentInsets = NSEdgeInsets(top: 0, left: 0, bottom: 34, right: 0)   // room for «Ответы · N»

        // sheet
        sheet.boxType = .custom
        sheet.cornerRadius = 14
        sheet.borderWidth = 0.5
        sheet.borderColor = .separatorColor
        sheet.fillColor = .windowBackgroundColor
        sheet.contentViewMargins = .zero
        let grabber = NSBox()
        grabber.boxType = .custom
        grabber.cornerRadius = 2
        grabber.borderWidth = 0
        grabber.fillColor = .tertiaryLabelColor
        sheetQuestion.font = .systemFont(ofSize: 11, weight: .medium)
        sheetQuestion.textColor = .tertiaryLabelColor
        sheetQuestion.lineBreakMode = .byTruncatingTail
        sheetQuestion.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        sheetMeta.font = .systemFont(ofSize: 11)
        sheetMeta.textColor = .tertiaryLabelColor
        let copy = iconButton("doc.on.doc", "Скопировать ответ", #selector(copyAnswer))
        let collapse = iconButton("chevron.down", "Свернуть ответы", #selector(collapseSheet))
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let head = NSStackView(views: [sheetQuestion, spacer, sheetMeta, copy, collapse])
        head.orientation = .horizontal
        head.spacing = 6
        head.alignment = .centerY
        let answersScroll = NSTextView.scrollableTextView()
        answersScroll.hasVerticalScroller = true
        answersScroll.borderType = .noBorder
        answersScroll.drawsBackground = false
        guard let answers = answersScroll.documentView as? NSTextView else { fatalError("scrollableTextView has a text view") }
        answers.isEditable = false
        answers.isSelectable = true
        answers.drawsBackground = false
        answers.textContainerInset = NSSize(width: 0, height: 4)
        answersView = answers
        let inner = NSView()
        for v in [grabber, head, answersScroll] {
            v.translatesAutoresizingMaskIntoConstraints = false
            inner.addSubview(v)
        }
        NSLayoutConstraint.activate([
            grabber.topAnchor.constraint(equalTo: inner.topAnchor, constant: 7),
            grabber.centerXAnchor.constraint(equalTo: inner.centerXAnchor),
            grabber.widthAnchor.constraint(equalToConstant: 36),
            grabber.heightAnchor.constraint(equalToConstant: 4),
            head.topAnchor.constraint(equalTo: grabber.bottomAnchor, constant: 8),
            head.leadingAnchor.constraint(equalTo: inner.leadingAnchor, constant: 16),
            head.trailingAnchor.constraint(equalTo: inner.trailingAnchor, constant: -10),
            answersScroll.topAnchor.constraint(equalTo: head.bottomAnchor, constant: 6),
            answersScroll.leadingAnchor.constraint(equalTo: inner.leadingAnchor, constant: 16),
            answersScroll.trailingAnchor.constraint(equalTo: inner.trailingAnchor, constant: -12),
            answersScroll.bottomAnchor.constraint(equalTo: inner.bottomAnchor, constant: -20),   // under the hidden lip
        ])
        sheet.contentView = inner

        reopen.bezelStyle = .rounded
        reopen.controlSize = .small
        reopen.image = NSImage(systemSymbolName: "chevron.up", accessibilityDescription: nil)
        reopen.imagePosition = .imageTrailing
        reopen.target = self
        reopen.action = #selector(reopenSheet)
        reopen.isHidden = true
        sheet.isHidden = true

        for v in [scroll, sheet, reopen] {
            v.translatesAutoresizingMaskIntoConstraints = false
            stage.addSubview(v)
        }
        let h = sheet.heightAnchor.constraint(equalTo: stage.heightAnchor, multiplier: 0.72, constant: 12)
        sheetHeight = h
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: stage.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: stage.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: stage.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: stage.bottomAnchor),
            sheet.leadingAnchor.constraint(equalTo: stage.leadingAnchor, constant: 6),
            sheet.trailingAnchor.constraint(equalTo: stage.trailingAnchor, constant: -6),
            sheet.bottomAnchor.constraint(equalTo: stage.bottomAnchor, constant: 12),   // bottom corners tucked away
            h,
            reopen.trailingAnchor.constraint(equalTo: stage.trailingAnchor, constant: -12),
            reopen.bottomAnchor.constraint(equalTo: stage.bottomAnchor, constant: -10),
        ])
        return stage
    }

    private func makeComposer() -> NSView {
        input.placeholderString = "Спросить про встречу…"
        input.bezelStyle = .roundedBezel
        input.font = .systemFont(ofSize: 13)
        input.target = self
        input.action = #selector(askFromField)
        input.setContentHuggingPriority(.defaultLow, for: .horizontal)
        contextSwitch.controlSize = .mini
        contextSwitch.target = self
        contextSwitch.action = #selector(toggleContext)
        contextSwitch.toolTip = "Агент видит заметки о проектах из папки контекста"
        contextLabel.font = .systemFont(ofSize: 11)
        contextLabel.textColor = .secondaryLabelColor
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        spinner.isHidden = true
        send.isBordered = false
        send.bezelStyle = .inline
        send.imageScaling = .scaleProportionallyUpOrDown
        send.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 20, weight: .regular)
        send.contentTintColor = .controlAccentColor
        send.target = self
        send.action = #selector(askOrStop)
        setupAgent.bezelStyle = .rounded
        setupAgent.target = self
        setupAgent.action = #selector(openAgentSettings)
        privateNote.font = .systemFont(ofSize: 12)
        privateNote.textColor = .secondaryLabelColor
        for v in [input, spinner, contextSwitch, contextLabel, send, setupAgent, privateNote] { composer.addArrangedSubview(v) }
        composer.orientation = .horizontal
        composer.spacing = 8
        composer.alignment = .centerY
        setRunning(false)
        return composer
    }

    private func iconButton(_ symbol: String, _ tip: String, _ action: Selector) -> NSButton {
        let b = NSButton(image: NSImage(systemSymbolName: symbol, accessibilityDescription: tip) ?? NSImage(),
                         target: self, action: action)
        b.isBordered = false
        b.bezelStyle = .inline
        b.toolTip = tip
        b.contentTintColor = .secondaryLabelColor
        b.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        return b
    }

    /// For `--transcript-test`: the panel as a PNG, without screen-recording permission.
    func snapshot(to url: URL) {
        guard let view = panel?.contentView?.superview ?? panel?.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
    }

    /// For `--transcript-test`: open the answers sheet as if a question had been asked.
    func openAnswersForSnapshot() { setSheet(open: true); renderAnswers() }

    @objc private func openNote() { onOpenNote?() }
    @objc private func stopRecording() { onStop?() }
    @objc private func hideButton() { onHide?() }

    /// A menu-bar app has no Edit menu, so ⌘C/⌘V/⌘X/⌘A/⌘W would go nowhere: the panel sends them
    /// to whatever has focus — the question field or the transcript.
    private final class KeyPanel: NSPanel {
        override var canBecomeKey: Bool { true }

        override func performKeyEquivalent(with event: NSEvent) -> Bool {
            guard event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command else {
                return super.performKeyEquivalent(with: event)
            }
            // by key position, not by character: on a Russian layout ⌘C types «с»
            let action: Selector?
            switch event.keyCode {
            case 8: action = #selector(NSText.copy(_:))
            case 9: action = #selector(NSText.paste(_:))
            case 7: action = #selector(NSText.cut(_:))
            case 0: action = #selector(NSText.selectAll(_:))
            case 6: action = Selector(("undo:"))
            case 13: performClose(nil); return true
            default: action = nil
            }
            if let action, NSApp.sendAction(action, to: nil, from: self) { return true }
            return super.performKeyEquivalent(with: event)
        }
    }
}
