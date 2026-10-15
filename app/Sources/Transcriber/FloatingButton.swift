import AppKit

/// Small always-on-top round button at the right edge of the screen. A click pops up a menu:
/// jumping straight into the note was a blind action — from the button there is now a way to see
/// how long the recording has been running, open the note, stop, or get the button out of the way.
final class FloatingButton {
    private var panel: NSPanel?
    /// The menu is built fresh on every click, so the elapsed time is current and not whatever it
    /// was when the session started.
    private var startedAt: Date?
    private var title: String?

    var onOpenNote: (() -> Void)?
    var onStop: (() -> Void)?
    var onHide: (() -> Void)?

    /// `private` paints the dot purple — the same signal the menu and the prompt card give, so a
    /// glance at the button is enough to know where this recording is going.
    func show(private isPrivate: Bool = false, title: String? = nil, startedAt: Date? = nil) {
        self.title = title
        self.startedAt = startedAt
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
        view.toolTip = "Идёт запись — нажми для действий"
        view.onClick = { [weak self] anchor in self?.popUpMenu(on: anchor) }
        p.contentView = view
        if let screen = NSScreen.main ?? NSScreen.screens.first {   // .main is nil with no key window
            let f = screen.visibleFrame
            p.setFrameOrigin(NSPoint(x: f.maxX - size - 8, y: f.midY))
        }
        p.orderFrontRegardless()
        panel = p
    }

    func hide() {
        panel?.orderOut(nil)
        panel = nil
        startedAt = nil
        title = nil
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
        let actions: [(String, Selector, (() -> Void)?)] = [
            ("Открыть заметку", #selector(Actions.openNote), onOpenNote),
            ("Остановить запись", #selector(Actions.stop), onStop),
        ]
        let target = Actions(openNote: onOpenNote, stop: onStop, hide: onHide)
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
        private let openNoteBlock: (() -> Void)?
        private let stopBlock: (() -> Void)?
        private let hideBlock: (() -> Void)?
        init(openNote: (() -> Void)?, stop: (() -> Void)?, hide: (() -> Void)?) {
            openNoteBlock = openNote
            stopBlock = stop
            hideBlock = hide
        }
        @objc func openNote() { openNoteBlock?() }
        @objc func stop() { stopBlock?() }
        @objc func hide() { hideBlock?() }
    }

    private final class DotView: NSView {
        var onClick: ((NSView) -> Void)?
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
            if hypot(up.x - downAt.x, up.y - downAt.y) < 4 { onClick?(self) }
        }
    }
}
