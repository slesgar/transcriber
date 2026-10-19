import AppKit
import TranscriberCore

/// Window with the text recognised so far, opened by a click on the floating button. It follows
/// the Raw the worker rewrites after every block (`LiveTranscriptFollower`) and carries the actions
/// that used to live in the button's menu, so the transcript and «stop» are one place.
///
/// Non-activating: the meeting app keeps focus, yet the panel can still become key, so the text
/// scrolls, selects and copies. Kept out of screen sharing — a live transcript on a shared screen
/// is exactly the leak a private recording exists to avoid.
final class TranscriptWindow: NSObject, NSWindowDelegate {
    var onOpenNote: (() -> Void)?
    var onStop: (() -> Void)?
    var onHide: (() -> Void)?

    private var panel: NSPanel?
    private var textView: NSTextView?
    private var elapsedLabel: NSTextField?
    private var timer: Timer?
    private var follower: LiveTranscriptFollower?
    private var startedAt: Date?
    /// The body on screen; nil — the placeholder (or nothing yet).
    private var shownBody: String?

    static let waitingText = "Текст появится, когда воркер распознает первый блок — обычно через минуту-две после начала речи."
    /// The follower only stats the Raw between rewrites, so a one-second tick is cheap and keeps
    /// the elapsed time honest.
    static let tick: TimeInterval = 1

    var isVisible: Bool { panel?.isVisible ?? false }
    var session: SessionDir? { follower?.session }

    func show(session: SessionDir, title: String?, startedAt: Date?, near anchor: NSRect?) {
        if follower?.session != session { forget() }
        if follower == nil { follower = LiveTranscriptFollower(session: session) }
        self.startedAt = startedAt
        let p = panel ?? makePanel()
        if let title, !title.isEmpty { p.title = title } else { p.title = "Транскрипт" }
        let opening = !p.isVisible
        if opening { place(p, near: anchor) }
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

    /// Closing only stops the ticking; the text and the follower stay, so reopening is instant.
    func close() {
        panel?.orderOut(nil)
        stopTicking()
    }

    /// Recording is over: forget the session so a later click cannot show a stale meeting.
    func reset() {
        close()
        forget()
    }

    func windowWillClose(_ notification: Notification) { stopTicking() }

    private func stopTicking() {
        timer?.invalidate()
        timer = nil
    }

    private func forget() {
        follower = nil
        startedAt = nil
        shownBody = nil
        textView?.string = ""
    }

    private func refresh() {
        elapsedLabel?.stringValue = "Запись · \(FloatingButton.elapsed(since: startedAt))"
        guard let textView, let now = follower?.poll() else { return }
        switch now {
        case .waiting:
            guard shownBody != nil || textView.string.isEmpty else { return }   // keep what was shown
            shownBody = nil
            textView.string = Self.waitingText
            textView.textColor = .secondaryLabelColor
        case .text(let body):
            show(body, in: textView)
        }
    }

    /// New replies are appended, so a selection in progress and the scroll position survive the
    /// worker's rewrite; only a real rewrite of earlier text replaces everything.
    private func show(_ body: String, in textView: NSTextView) {
        guard body != shownBody else { return }
        let atBottom = Self.isScrolledToBottom(textView)
        let attrs: [NSAttributedString.Key: Any] = [.font: Self.font, .foregroundColor: NSColor.labelColor]
        if let old = shownBody, let tail = LiveTranscript.appendix(from: old, to: body),
           let storage = textView.textStorage {
            storage.append(NSAttributedString(string: tail, attributes: attrs))
        } else {
            let origin = textView.enclosingScrollView?.contentView.bounds.origin
            textView.textStorage?.setAttributedString(NSAttributedString(string: body, attributes: attrs))
            if !atBottom, let origin, let clip = textView.enclosingScrollView?.contentView {
                clip.scroll(to: origin)
                textView.enclosingScrollView?.reflectScrolledClipView(clip)
            }
        }
        shownBody = body
        if atBottom { textView.scrollToEndOfDocument(nil) }
    }

    private static let font = NSFont.systemFont(ofSize: 13)

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
        let p = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 520),
                         styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        p.level = .floating
        p.isFloatingPanel = true
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        p.sharingType = .none
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.minSize = NSSize(width: 460, height: 220)   // the bar needs the width for all three buttons
        p.delegate = self

        let scroll = NSTextView.scrollableTextView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder
        guard let text = scroll.documentView as? NSTextView else { fatalError("scrollableTextView has a text view") }
        text.isEditable = false
        text.isSelectable = true
        text.isRichText = false
        text.font = Self.font
        text.textContainerInset = NSSize(width: 8, height: 8)
        p.textView = text

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

    /// A menu-bar app has no Edit menu, so ⌘C/⌘A/⌘W would go nowhere: the panel handles them itself.
    private final class KeyPanel: NSPanel {
        weak var textView: NSTextView?

        override func performKeyEquivalent(with event: NSEvent) -> Bool {
            guard event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command else {
                return super.performKeyEquivalent(with: event)
            }
            switch event.charactersIgnoringModifiers {
            case "c": textView?.copy(nil); return true
            case "a": textView?.selectAll(nil); return true
            case "w": performClose(nil); return true
            default: return super.performKeyEquivalent(with: event)
            }
        }
    }
}
