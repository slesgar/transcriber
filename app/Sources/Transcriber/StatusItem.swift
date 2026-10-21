import AppKit

final class StatusItem {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let statusLine = NSMenuItem(title: "Ожидание встречи", action: nil, keyEquivalent: "")
    private let toggle = NSMenuItem(title: "Начать запись", action: #selector(toggleAction), keyEquivalent: "r")
    private let openNote = NSMenuItem(title: "Открыть заметку", action: #selector(openNoteAction), keyEquivalent: "o")
    /// Brings back the floating button hidden with «Скрыть кружок» — the way to the meeting window.
    private let showButton = NSMenuItem(title: "Показать кружок", action: #selector(showButtonAction), keyEquivalent: "")
    /// Checkbox: the schedule catches the regular appointments, this catches everything else —
    /// one-shot, so it never stays on by accident.
    private let privateNext = NSMenuItem(title: "Следующая запись приватная",
                                         action: #selector(privateNextAction), keyEquivalent: "")
    /// What the status line says when no transient hint is shown; `setHint(nil)` returns to it.
    private var baseStatus = "Ожидание встречи"

    var onToggle: (() -> Void)?
    var onOpenNote: (() -> Void)?
    var onShowButton: (() -> Void)?
    var onOpenSettings: (() -> Void)?
    var onOpenSessions: (() -> Void)?
    var onTogglePrivateNext: (() -> Void)?

    init() {
        let menu = NSMenu()
        menu.autoenablesItems = false   // without this AppKit re-enables items by target/action lookup
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())
        for m in [toggle, privateNext, openNote, showButton] { m.target = self; menu.addItem(m) }
        showButton.isHidden = true
        let settings = NSMenuItem(title: "Настройки…", action: #selector(openSettingsAction), keyEquivalent: ",")
        settings.target = self
        menu.addItem(settings)
        let sessions = NSMenuItem(title: "Папка сессий", action: #selector(openSessionsAction), keyEquivalent: "")
        sessions.target = self
        menu.addItem(sessions)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Выход", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        item.menu = menu
        setRecording(false, detail: nil)
    }

    func setRecording(_ recording: Bool, detail: String?, private isPrivate: Bool = false) {
        if recording {
            setIcon("record.circle.fill", tint: isPrivate ? .systemPurple : .systemRed)
        } else {
            setIcon("waveform.circle", tint: nil)
        }
        let what = isPrivate ? "Идёт приватная запись" : "Идёт запись"
        baseStatus = recording ? what + (detail.map { " · \($0)" } ?? "") : "Ожидание встречи"
        statusLine.title = baseStatus
        toggle.title = recording ? "Остановить запись" : "Начать запись"
        openNote.isEnabled = recording
        if !recording { showButton.isHidden = true }
    }

    /// Shown only while a recording runs with its button hidden.
    func setButtonHidden(_ hidden: Bool) {
        showButton.isHidden = !hidden
    }

    /// The one-shot override is spent by the session that used it, so the checkbox follows the
    /// controller rather than its own click.
    func setPrivateNext(_ on: Bool) {
        privateNext.state = on ? .on : .off
    }

    /// Transient line of text in the menu: the glyph and the menu items stay as they are.
    func setHint(_ text: String?) {
        statusLine.title = text ?? baseStatus
    }

    func setProblem(_ text: String) {
        setIcon("exclamationmark.circle.fill", tint: .systemOrange)
        statusLine.title = text
    }

    /// A glyph in the title is drawn in the label colour, which on a dark or dimmed menu bar (an
    /// inactive display) is all but invisible. An SF Symbol image is not: as a template macOS
    /// recolours it for whatever the bar is doing, and the coloured states carry their own palette
    /// so they read the same everywhere.
    private func setIcon(_ symbol: String, tint: NSColor?) {
        var config = NSImage.SymbolConfiguration(pointSize: 15, weight: .medium)
        if let tint { config = config.applying(NSImage.SymbolConfiguration(paletteColors: [tint])) }
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(config)
        image?.isTemplate = tint == nil
        item.button?.image = image
        item.button?.contentTintColor = tint
        item.button?.title = ""
    }

    @objc private func toggleAction() { onToggle?() }
    @objc private func privateNextAction() { onTogglePrivateNext?() }
    @objc private func openNoteAction() { onOpenNote?() }
    @objc private func showButtonAction() { onShowButton?() }
    @objc private func openSettingsAction() { onOpenSettings?() }
    @objc private func openSessionsAction() { onOpenSessions?() }
}
