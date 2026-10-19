import AppKit
import TranscriberCore

/// Window with the text recognised so far, opened by a click on the floating button. It reads the
/// Raw the worker rewrites after every block (`LiveTranscript`) and carries the actions that used
/// to live in the button's menu, so the transcript and «stop» are one place.
///
/// Non-activating: the meeting app keeps focus, yet the panel can still become key, so the text
/// scrolls, selects and copies.
final class TranscriptWindow: NSObject, NSWindowDelegate {
    var onOpenNote: (() -> Void)?
    var onStop: (() -> Void)?
    var onHide: (() -> Void)?

    private var panel: NSPanel?
    private var textView: NSTextView?
    private var elapsedLabel: NSTextField?
    private var timer: Timer?
    private var session: SessionDir?
    private var startedAt: Date?
    private var shown: LiveTranscript?

    static let waitingText = "Текст появится, когда воркер распознает первый блок — обычно через минуту-две после начала речи."
    /// Raw is rewritten once per recognised block (45–90 s of speech), so two seconds is plenty.
    static let refreshEvery: TimeInterval = 2

    var isVisible: Bool { panel?.isVisible ?? false }

    func show(session: SessionDir, title: String?, startedAt: Date?, near anchor: NSRect?) {
        self.session = session
        self.startedAt = startedAt
        let p = panel ?? makePanel()
        p.title = (title?.isEmpty == false) ? title! : "Транскрипт"
        let opening = !p.isVisible
        if opening { place(p, near: anchor) }
        refresh()
        if opening { textView?.scrollToEndOfDocument(nil) }   // a fresh look starts at the latest line
        p.orderFrontRegardless()
        p.makeKey()
        if timer == nil {
            let t = Timer(timeInterval: Self.refreshEvery, repeats: true) { [weak self] _ in self?.refresh() }
            RunLoop.main.add(t, forMode: .common)
            timer = t
        }
    }

    func close() {
        timer?.invalidate()
        timer = nil
        panel?.orderOut(nil)
        shown = nil
    }

    /// Recording is over: forget the session so a later click cannot show a stale meeting.
    func reset() {
        close()
        session = nil
        startedAt = nil
    }

    func windowWillClose(_ notification: Notification) {
        timer?.invalidate()
        timer = nil
        shown = nil
    }

    private func refresh() {
        elapsedLabel?.stringValue = "Запись · \(FloatingButton.elapsed(since: startedAt))"
        guard let session, let textView else { return }
        let now = LiveTranscript.read(session)
        guard now != shown else { return }
        shown = now
        let atBottom = Self.isScrolledToBottom(textView)
        switch now {
        case .waiting:
            textView.string = Self.waitingText
            textView.textColor = .secondaryLabelColor
        case .text(let body):
            textView.string = body
            textView.textColor = .labelColor
        }
        if atBottom { textView.scrollToEndOfDocument(nil) }
    }

    /// Follow new lines only when the reader is already at the end: scrolled up = reading, leave it.
    private static func isScrolledToBottom(_ textView: NSTextView) -> Bool {
        guard let scroll = textView.enclosingScrollView else { return true }
        let visible = scroll.contentView.documentVisibleRect
        return visible.maxY >= textView.bounds.height - 24
    }

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
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 440, height: 520),
                        styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        p.level = .floating
        p.isFloatingPanel = true
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.minSize = NSSize(width: 300, height: 220)
        p.delegate = self

        let scroll = NSTextView.scrollableTextView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder
        let text = scroll.documentView as! NSTextView
        text.isEditable = false
        text.isSelectable = true
        text.isRichText = false
        text.font = .systemFont(ofSize: 13)
        text.textContainerInset = NSSize(width: 8, height: 8)

        let elapsed = NSTextField(labelWithString: "")
        elapsed.textColor = .secondaryLabelColor
        elapsed.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        let open = button("Открыть заметку", #selector(openNote))
        let stop = button("Остановить запись", #selector(stopRecording))
        let hide = button("Скрыть кнопку", #selector(hideButton))
        hide.toolTip = "Убрать кружок до конца записи"
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let bar = NSStackView(views: [elapsed, spacer, hide, open, stop])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.edgeInsets = NSEdgeInsets(top: 8, left: 10, bottom: 10, right: 10)

        let root = NSStackView(views: [scroll, bar])
        root.orientation = .vertical
        root.spacing = 0
        root.alignment = .leading
        scroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        bar.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        p.contentView = root

        panel = p
        textView = text
        elapsedLabel = elapsed
        return p
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        b.controlSize = .small
        return b
    }

    /// For `--transcript-test`: the panel as a PNG, without screen-recording permission.
    func snapshot(to url: URL) {
        guard let view = panel?.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
    }

    @objc private func openNote() { onOpenNote?() }
    @objc private func stopRecording() { onStop?() }
    @objc private func hideButton() { onHide?() }
}
