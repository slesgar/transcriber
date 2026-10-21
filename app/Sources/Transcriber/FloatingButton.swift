import AppKit
import TranscriberCore

/// Small always-on-top round button at the right edge of the screen. A click opens the window with
/// the text recognised so far (`TranscriptWindow`), which also carries the actions: open the note,
/// stop, get the button out of the way. A right click (or ctrl-click) pops up the same actions as
/// a menu, together with how long the recording has been running.
final class FloatingButton {
    private var panel: NSPanel?
    private let transcript = TranscriptWindow()
    private var session: SessionDir?
    /// The menu is built fresh on every click, so the elapsed time is current and not whatever it
    /// was when the session started.
    private var startedAt: Date?
    private var title: String?
    /// The recording is private: the meeting window then offers no agent.
    private var isPrivate = false

    var onOpenNote: (() -> Void)? { didSet { transcript.onOpenNote = onOpenNote } }
    var onStop: (() -> Void)? { didSet { transcript.onStop = onStop } }
    var onHide: (() -> Void)? { didSet { transcript.onHide = onHide } }

    /// `private` paints the dot purple — the same signal the menu and the prompt card give, so a
    /// glance at the button is enough to know where this recording is going.
    func show(private isPrivate: Bool = false, title: String? = nil, startedAt: Date? = nil,
              session: SessionDir? = nil) {
        self.title = title
        self.startedAt = startedAt
        self.isPrivate = isPrivate
        if session != self.session, transcript.session != nil, transcript.session != session {
            transcript.reset()   // a new recording without an idle in between: never show the old one
        }
        self.session = session
        if let panel, let view = panel.contentView as? DotView {
            view.isPrivate = isPrivate
            return
        }
        let size: CGFloat = 44
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: size, height: size),
                        styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        p.level = .floating
        p.isFloatingPanel = true
        p.hidesOnDeactivate = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.isMovableByWindowBackground = true
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let view = DotView(frame: NSRect(x: 0, y: 0, width: size, height: size))
        view.isPrivate = isPrivate
        view.toolTip = "Идёт запись — нажми, чтобы увидеть текст; правый клик — действия"
        view.onClick = { [weak self] _ in self?.toggleTranscript() }
        view.onMenu = { [weak self] anchor in self?.popUpMenu(on: anchor) }
        p.contentView = view
        if let screen = NSScreen.main ?? NSScreen.screens.first {   // .main is nil with no key window
            let f = screen.visibleFrame
            p.setFrameOrigin(NSPoint(x: f.maxX - size - 8, y: f.midY))
        }
        p.orderFrontRegardless()
        panel = p
    }

    func hide() {
        transcript.reset()
        panel?.orderOut(nil)
        panel = nil
        startedAt = nil
        title = nil
        session = nil
    }

    private func toggleTranscript() {
        if transcript.isVisible {
            transcript.close()
            return
        }
        guard let session else { return }
        transcript.show(session: session, title: title, startedAt: startedAt, near: panel?.frame, private: isPrivate)
    }

    private func popUpMenu(on view: NSView) {
        let menu = NSMenu()
        menu.autoenablesItems = false   // otherwise AppKit re-enables by target/action lookup
        let header = NSMenuItem(title: "Запись · \(Self.elapsed(since: startedAt))", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        if let title, !title.isEmpty {
            let name = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            name.isEnabled = false
            menu.addItem(name)
        }
        menu.addItem(.separator())
        let showText: (() -> Void)? = session == nil ? nil : { [weak self] in self?.toggleTranscript() }
        let actions: [(String, Selector, (() -> Void)?)] = [
            (transcript.isVisible ? "Скрыть текст" : "Показать текст", #selector(Actions.showText), showText),
            ("Открыть заметку", #selector(Actions.openNote), onOpenNote),
            ("Остановить запись", #selector(Actions.stop), onStop),
        ]
        let target = Actions(showText: showText, openNote: onOpenNote, stop: onStop, hide: onHide)
        for (title, selector, block) in actions {
            let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
            item.target = target
            item.isEnabled = block != nil
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let hideItem = NSMenuItem(title: "Скрыть кнопку до конца записи", action: #selector(Actions.hide), keyEquivalent: "")
        hideItem.target = target
        menu.addItem(hideItem)
        // The target is retained only by the menu items, so it has to outlive the pop-up: `popUp`
        // is modal, so holding it in this frame is enough.
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: view.bounds.height + 4), in: view)
        withExtendedLifetime(target) {}
    }

    /// «MM:SS», and hours when a meeting really does run that long. No start time = «—».
    static func elapsed(since start: Date?, now: Date = Date()) -> String {
        guard let start else { return "—" }
        let seconds = Int(max(now.timeIntervalSince(start), 0))
        let (h, m, s) = (seconds / 3600, (seconds % 3600) / 60, seconds % 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%02d:%02d", m, s)
    }

    /// Menu items need an ObjC target; the closures live here for the life of the pop-up.
    private final class Actions: NSObject {
        private let showTextBlock: (() -> Void)?
        private let openNoteBlock: (() -> Void)?
        private let stopBlock: (() -> Void)?
        private let hideBlock: (() -> Void)?
        init(showText: (() -> Void)?, openNote: (() -> Void)?, stop: (() -> Void)?, hide: (() -> Void)?) {
            showTextBlock = showText
            openNoteBlock = openNote
            stopBlock = stop
            hideBlock = hide
        }
        @objc func showText() { showTextBlock?() }
        @objc func openNote() { openNoteBlock?() }
        @objc func stop() { stopBlock?() }
        @objc func hide() { hideBlock?() }
    }

    private final class DotView: NSView {
        var onClick: ((NSView) -> Void)?
        var onMenu: ((NSView) -> Void)?
        var isPrivate = false { didSet { if isPrivate != oldValue { needsDisplay = true } } }
        private var downAt: NSPoint = .zero

        override func draw(_ dirtyRect: NSRect) {
            let bg = NSBezierPath(ovalIn: bounds.insetBy(dx: 2, dy: 2))
            NSColor.windowBackgroundColor.withAlphaComponent(0.95).setFill()
            bg.fill()
            NSColor.separatorColor.setStroke()
            bg.lineWidth = 1
            bg.stroke()
            let dot = NSBezierPath(ovalIn: NSRect(x: bounds.midX - 7, y: bounds.midY + 3, width: 14, height: 14))
            (isPrivate ? NSColor.systemPurple : NSColor.systemRed).setFill()
            dot.fill()
            let text = "✎" as NSString
            let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 13), .foregroundColor: NSColor.labelColor]
            let s = text.size(withAttributes: attrs)
            text.draw(at: NSPoint(x: bounds.midX - s.width / 2, y: bounds.midY - 15), withAttributes: attrs)
        }

        override func mouseDown(with event: NSEvent) {
            downAt = NSEvent.mouseLocation
            super.mouseDown(with: event)   // lets isMovableByWindowBackground drag the panel
        }

        override func mouseUp(with event: NSEvent) {
            let up = NSEvent.mouseLocation
            guard hypot(up.x - downAt.x, up.y - downAt.y) < 4 else { return }   // it was a drag
            if event.modifierFlags.contains(.control) { onMenu?(self) } else { onClick?(self) }
        }

        override func rightMouseDown(with event: NSEvent) { onMenu?(self) }
    }
}
