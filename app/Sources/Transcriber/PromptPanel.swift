import AppKit
import TranscriberCore

/// «В Толке началась встреча. Записать?» — a borderless, translucent card that slides in at the
/// top-right corner, auto-dismisses after `Spec.timeout`. Non-activating: it never steals focus from the call.
final class PromptPanel {
    private var panel: NSPanel?
    private var timer: Timer?
    private var handler: Handler?

    static let width: CGFloat = 420
    static let height: CGFloat = 128
    static let margin: CGFloat = 16
    /// Width left for the title and subtitle: card minus both insets, the 40-pt badge and its gap.
    static let textWidth: CGFloat = width - 20 - 40 - 14 - 20

    struct Spec {
        var symbol: String
        var tint: NSColor
        var title: String
        var subtitle: String
        var primary: String
        var secondary: String
        var timeout: TimeInterval
    }

    /// With a known meeting name the card leads with it — «Встреча в браузере» says nothing when
    /// the browser has already told us which call it is.
    static func meeting(appName: String, title: String? = nil) -> Spec {
        if let title, !title.isEmpty {
            return Spec(symbol: "record.circle", tint: .systemRed, title: title,
                        subtitle: "В браузере идёт звонок. Записать и завести заметку?",
                        primary: "Записать", secondary: "Не сейчас", timeout: PromptTimeouts.meeting)
        }
        return Spec(symbol: "record.circle", tint: .systemRed, title: "Встреча в \(appName)",
                    subtitle: "Записать разговор и завести заметку в вольте?", primary: "Записать", secondary: "Не сейчас", timeout: PromptTimeouts.meeting)
    }

    /// A private session gets its own card and no meeting name at all: the point is to show, before
    /// the first word is recorded, that this one is not going into the work vault.
    static func privateMeeting() -> Spec {
        Spec(symbol: "lock.circle", tint: .systemPurple, title: "Приватная запись",
             subtitle: "Транскрипт уйдёт в «Приватные записи», не в рабочий вольт.",
             primary: "Записать", secondary: "Не сейчас", timeout: PromptTimeouts.privateMeeting)
    }

    static func stop(minutes: Int) -> Spec {
        Spec(symbol: "moon.zzz", tint: .systemOrange, title: "Похоже, встреча закончилась",
             subtitle: "Тишина уже \(minutes) мин. Остановить запись?", primary: "Остановить", secondary: "Продолжить", timeout: 0)
    }

    var isShowing: Bool { panel != nil }

    /// Any card. `timeout` 0 = stays until answered or dismissed; otherwise `onTimeout` fires when
    /// it elapses — the secondary action unless the caller wants silence to mean something else.
    func show(_ spec: Spec, onPrimary: @escaping () -> Void, onSecondary: @escaping () -> Void,
              onTimeout: (() -> Void)? = nil) {
        let onElapsed = onTimeout ?? onSecondary
        dismiss(animated: false)
        let onRecord = onPrimary
        let onSkip = onSecondary
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: Self.width, height: Self.height),
                        styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        p.level = .floating
        p.isFloatingPanel = true
        p.hidesOnDeactivate = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.isMovableByWindowBackground = true
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let handler = Handler(record: { [weak self] in self?.dismiss(); onRecord() },
                              skip: { [weak self] in self?.dismiss(); onSkip() })
        self.handler = handler
        p.contentView = Card(spec: spec, handler: handler)

        guard let screen = Self.screenUnderCursor() else { return }
        let visible = screen.visibleFrame
        let size = NSSize(width: Self.width, height: Self.height)
        let wanted = NSPoint(x: visible.maxX - Self.width - Self.margin, y: visible.maxY - Self.height - Self.margin)
        let target = Self.clampedOrigin(size: size, target: wanted, in: visible, margin: Self.margin)
        p.setFrameOrigin(NSPoint(x: target.x + 40, y: target.y))
        p.alphaValue = 0
        p.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.28
            ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
            p.animator().alphaValue = 1
            p.animator().setFrameOrigin(target)
        }
        panel = p

        guard spec.timeout > 0 else { return }
        // .common, so an open menu or a window resize does not stall the auto-dismiss
        let t = Timer(timeInterval: spec.timeout, repeats: false) { [weak self] _ in self?.dismiss(); onElapsed() }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    /// The card belongs where the eyes are. `NSScreen.main` is the screen with the key window, and
    /// this panel never takes focus — on two displays that put the card on whichever screen last
    /// had a window, often the wrong one and partly off its edge.
    static func screenUnderCursor() -> NSScreen? {
        let loc = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(loc, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens.first
    }

    /// Pure: the origin closest to `target` that keeps a `size` card fully inside `visible` with
    /// `margin` free on every side. A screen too small for the card pins it to the bottom-left
    /// margin rather than returning something outside the frame.
    static func clampedOrigin(size: NSSize, target: NSPoint, in visible: NSRect, margin: CGFloat) -> NSPoint {
        let minX = visible.minX + margin
        let maxX = visible.maxX - margin - size.width
        let minY = visible.minY + margin
        let maxY = visible.maxY - margin - size.height
        return NSPoint(x: minX <= maxX ? min(max(target.x, minX), maxX) : minX,
                       y: minY <= maxY ? min(max(target.y, minY), maxY) : minY)
    }

    /// Renders the card to a PNG (layout check without screen-recording permission).
    func snapshot(to url: URL) {
        guard let view = panel?.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
    }

    func dismiss(animated: Bool = true) {
        timer?.invalidate()
        timer = nil
        handler = nil
        guard let p = panel else { return }
        panel = nil
        if animated {
            NSAnimationContext.runAnimationGroup({ ctx in
                ctx.duration = 0.18
                p.animator().alphaValue = 0
            }, completionHandler: { p.orderOut(nil) })
        } else {
            p.orderOut(nil)
        }
    }

    private final class Handler: NSObject {
        let recordBlock: () -> Void
        let skipBlock: () -> Void
        init(record: @escaping () -> Void, skip: @escaping () -> Void) {
            recordBlock = record
            skipBlock = skip
        }
        @objc func record(_ sender: Any?) { recordBlock() }
        @objc func skip(_ sender: Any?) { skipBlock() }
    }

    /// The accent-filled primary action.
    ///
    /// A stock `.rounded` button with `bezelColor` is drawn grey whenever its window is not the
    /// main one — and this panel is non-activating by design, so that is always. The fill is
    /// therefore drawn by hand from the same `bezelColor`, which keeps «Записать» looking like the
    /// thing to click.
    private final class PrimaryButton: NSButton {
        init(title: String, target: AnyObject, action: Selector) {
            super.init(frame: .zero)
            self.title = title
            self.target = target
            self.action = action
            bezelStyle = .rounded
            isBordered = false
            wantsLayer = true
            layer?.cornerRadius = 8
            layer?.cornerCurve = .continuous
            applyStyle()
        }

        required init?(coder: NSCoder) { nil }

        override var bezelColor: NSColor? { didSet { applyStyle() } }
        override var isHighlighted: Bool { didSet { applyStyle() } }

        override func viewDidChangeEffectiveAppearance() {
            super.viewDidChangeEffectiveAppearance()
            applyStyle()
        }

        private func applyStyle() {
            let fill = bezelColor ?? .controlAccentColor
            // `cgColor` freezes the dynamic colour, so it is re-read on every appearance change.
            layer?.backgroundColor = (isHighlighted ? fill.withAlphaComponent(0.78) : fill).cgColor
            attributedTitle = NSAttributedString(string: title, attributes: [
                .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
                .foregroundColor: NSColor.white,
            ])
        }

        override var intrinsicContentSize: NSSize {
            NSSize(width: super.intrinsicContentSize.width + 24, height: 30)
        }
    }

    /// The SF Symbol on a tinted disc: the same shape for every card, so the colour alone says
    /// what kind of card this is.
    private final class IconBadge: NSView {
        private let symbol: String
        private let tint: NSColor

        init(symbol: String, tint: NSColor) {
            self.symbol = symbol
            self.tint = tint
            super.init(frame: NSRect(x: 0, y: 0, width: 40, height: 40))
        }

        required init?(coder: NSCoder) { nil }

        override func draw(_ dirtyRect: NSRect) {
            let disc = NSBezierPath(ovalIn: bounds)
            tint.withAlphaComponent(0.18).setFill()
            disc.fill()
            // The palette configuration bakes the tint into the symbol: drawing a template image
            // and then filling `.sourceAtop` would repaint the disc underneath it too.
            let config = NSImage.SymbolConfiguration(pointSize: 20, weight: .medium)
                .applying(NSImage.SymbolConfiguration(paletteColors: [tint]))
            guard let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
                    .withSymbolConfiguration(config) else { return }
            let size = image.size
            let origin = NSPoint(x: (bounds.width - size.width) / 2, y: (bounds.height - size.height) / 2)
            image.draw(in: NSRect(origin: origin, size: size))
        }
    }

    /// Rounded translucent card: badge, title, subtitle and two buttons.
    private final class Card: NSVisualEffectView {
        init(spec: Spec, handler: Handler) {
            super.init(frame: NSRect(x: 0, y: 0, width: PromptPanel.width, height: PromptPanel.height))
            // `.withinWindow` rather than `.behindWindow`: the card sits over a video call, where
            // blurring whatever is underneath costs legibility — and it makes `snapshot(to:)` show
            // the real thing instead of an empty rectangle.
            material = .hudWindow
            blendingMode = .withinWindow
            state = .active
            wantsLayer = true
            layer?.cornerRadius = 18
            layer?.cornerCurve = .continuous
            layer?.masksToBounds = true
            layer?.borderWidth = 1
            applyBorderColor()

            let icon = IconBadge(symbol: spec.symbol, tint: spec.tint)
            icon.translatesAutoresizingMaskIntoConstraints = false

            let title = NSTextField(labelWithString: spec.title)
            title.font = .systemFont(ofSize: 15, weight: .semibold)
            title.textColor = .labelColor
            title.lineBreakMode = .byTruncatingTail
            title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            title.translatesAutoresizingMaskIntoConstraints = false

            let subtitle = NSTextField(wrappingLabelWithString: spec.subtitle)
            subtitle.font = .systemFont(ofSize: 12.5)
            subtitle.textColor = .secondaryLabelColor
            subtitle.maximumNumberOfLines = 2
            // `.byTruncatingTail` here would turn the label back into a single line; wrapping plus
            // `truncatesLastVisibleLine` is what caps it at two and puts the ellipsis on the second.
            subtitle.lineBreakMode = .byWordWrapping
            subtitle.cell?.truncatesLastVisibleLine = true
            subtitle.isSelectable = false
            // Without an explicit wrapping width the label measures itself on one endless line and
            // then truncates inside the constraint — «…не в раб…» instead of a second line.
            subtitle.preferredMaxLayoutWidth = PromptPanel.textWidth
            subtitle.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            subtitle.translatesAutoresizingMaskIntoConstraints = false

            // Borderless text button: next to the filled primary it reads as the quiet way out,
            // which is what «Не сейчас» is.
            let skip = NSButton(title: spec.secondary, target: handler, action: #selector(Handler.skip(_:)))
            skip.bezelStyle = .rounded
            skip.isBordered = false
            skip.controlSize = .large
            skip.attributedTitle = NSAttributedString(string: spec.secondary, attributes: [
                .font: NSFont.systemFont(ofSize: 13, weight: .medium),
                .foregroundColor: NSColor.secondaryLabelColor,
            ])
            skip.translatesAutoresizingMaskIntoConstraints = false

            let record = PrimaryButton(title: spec.primary, target: handler, action: #selector(Handler.record(_:)))
            record.controlSize = .large
            record.bezelColor = .controlAccentColor
            // The panel never activates, so a key equivalent would only make the button pulse at
            // a keystroke it can never receive.
            record.keyEquivalent = ""
            record.translatesAutoresizingMaskIntoConstraints = false

            for v in [icon, title, subtitle, skip, record] as [NSView] { addSubview(v) }
            NSLayoutConstraint.activate([
                icon.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 20),
                icon.topAnchor.constraint(equalTo: topAnchor, constant: 18),
                icon.widthAnchor.constraint(equalToConstant: 40),
                icon.heightAnchor.constraint(equalToConstant: 40),

                title.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 14),
                title.topAnchor.constraint(equalTo: topAnchor, constant: 18),
                title.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -20),

                subtitle.leadingAnchor.constraint(equalTo: title.leadingAnchor),
                subtitle.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 3),
                subtitle.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -20),

                record.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -18),
                record.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -16),
                record.widthAnchor.constraint(greaterThanOrEqualToConstant: 108),
                record.topAnchor.constraint(greaterThanOrEqualTo: subtitle.bottomAnchor, constant: 6),

                skip.trailingAnchor.constraint(equalTo: record.leadingAnchor, constant: -6),
                skip.centerYAnchor.constraint(equalTo: record.centerYAnchor),
                skip.widthAnchor.constraint(greaterThanOrEqualToConstant: 92),
            ])
        }

        required init?(coder: NSCoder) { nil }

        /// A hairline that lifts the card off whatever is behind it — light on dark, dark on light.
        private func applyBorderColor() {
            let dark = effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            layer?.borderColor = (dark ? NSColor.white.withAlphaComponent(0.12)
                                       : NSColor.black.withAlphaComponent(0.08)).cgColor
        }

        override func viewDidChangeEffectiveAppearance() {
            super.viewDidChangeEffectiveAppearance()
            applyBorderColor()
        }
    }
}
