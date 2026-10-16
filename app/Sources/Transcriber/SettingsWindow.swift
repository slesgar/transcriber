import AppKit
import TranscriberCore

/// «Настройки…»: the owner, the two paths, meeting hosts and the privacy rules on one form.
/// Saving writes `config.yaml` and `app-config.json` through `SettingsStore`; the worker re-reads
/// its config when the next session starts (воркер перечитывает конфиг при старте следующей
/// сессии), the app at its next read, so nothing has to be restarted.
final class SettingsWindow: NSWindowController {
    static let shared = SettingsWindow()

    private let ownerName = NSTextField()
    private let ownerTalkName = NSTextField()
    private let vaultRoot = NSTextField()
    private let privateDir = NSTextField()
    private let workHosts = NSTextField()
    private let privateApps = NSTextField()
    /// `scrollableTextView()` hands back a scroll view with the text view already sized to track it.
    private let windowsScroll = NSTextView.scrollableTextView()
    private var windowsView: NSTextView { windowsScroll.documentView as! NSTextView }

    private init() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 420),
                              styleMask: [.titled, .closable], backing: .buffered, defer: true)
        window.title = "Настройки"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        buildContent(in: window)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// A menu-bar app has no Dock icon and is not active when its menu is used: without the
    /// activation the window would open behind the frontmost app.
    func show() {
        fill(SettingsStore.load())
        NSApp.activate(ignoringOtherApps: true)
        window?.center()
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }

    private func buildContent(in window: NSWindow) {
        for field in [ownerName, ownerTalkName, vaultRoot, privateDir, workHosts, privateApps] {
            field.translatesAutoresizingMaskIntoConstraints = false
            field.widthAnchor.constraint(greaterThanOrEqualToConstant: 380).isActive = true
        }
        workHosts.placeholderString = "talk.example.com, meet.example.com"
        privateApps.placeholderString = "com.example.chat"

        windowsView.isRichText = false
        windowsView.font = .monospacedSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        windowsView.isAutomaticQuoteSubstitutionEnabled = false
        windowsView.isAutomaticDashSubstitutionEnabled = false
        windowsView.isAutomaticTextReplacementEnabled = false
        let scroll = windowsScroll
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.heightAnchor.constraint(equalToConstant: 110).isActive = true

        let grid = NSGridView(views: [
            [label("Имя владельца"), ownerName],
            [label("Имя в Толке"), ownerTalkName],
            [label("Вольт (путь)"), vaultRoot],
            [label("Папка приватных записей"), privateDir],
            [label("Хосты встреч (через запятую)"), workHosts],
            [label("Приватные приложения (bundle id через запятую)"), privateApps],
            [label("Окна приватности (по строке: день HH:mm-HH:mm, 1=вс … 7=сб)"), scroll],
        ])
        grid.column(at: 0).xPlacement = .trailing
        grid.rowAlignment = .firstBaseline
        grid.row(at: 6).yPlacement = .top
        grid.columnSpacing = 10
        grid.rowSpacing = 10

        let cancel = NSButton(title: "Отмена", target: self, action: #selector(cancelAction))
        cancel.keyEquivalent = "\u{1b}"
        let save = NSButton(title: "Сохранить", target: self, action: #selector(saveAction))
        save.keyEquivalent = "\r"
        let buttons = NSStackView(views: [cancel, save])
        buttons.spacing = 10

        let stack = NSStackView(views: [grid, buttons])
        stack.orientation = .vertical
        stack.alignment = .trailing
        stack.spacing = 16
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView()
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])
        window.contentView = content
    }

    private func label(_ text: String) -> NSTextField {
        let l = NSTextField(labelWithString: text)
        l.alignment = .right
        return l
    }

    private func fill(_ s: Settings) {
        ownerName.stringValue = s.ownerName
        ownerTalkName.stringValue = s.ownerTalkName
        vaultRoot.stringValue = s.vaultRoot
        privateDir.stringValue = s.privateDir
        workHosts.stringValue = s.workHosts.joined(separator: ", ")
        privateApps.stringValue = s.privateApps.joined(separator: ", ")
        windowsView.string = s.privateWindows.map(\.line).joined(separator: "\n")
    }

    @objc private func cancelAction() { close() }

    @objc private func saveAction() {
        var windows: [PrivateWindow] = []
        var rejected: [String] = []
        for raw in windowsView.string.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { continue }
            if let w = PrivateWindow.parse(line: line) { windows.append(w) } else { rejected.append(line) }
        }
        if !rejected.isEmpty {
            alert("Не разобраны окна приватности",
                  "Формат строки: день HH:mm-HH:mm, день 1=вс … 7=сб, например «3 12:00-12:50».\n\n"
                      + rejected.joined(separator: "\n"))
            return
        }
        let s = Settings(ownerName: ownerName.stringValue.trimmingCharacters(in: .whitespaces),
                         ownerTalkName: ownerTalkName.stringValue.trimmingCharacters(in: .whitespaces),
                         vaultRoot: vaultRoot.stringValue.trimmingCharacters(in: .whitespaces),
                         privateDir: privateDir.stringValue.trimmingCharacters(in: .whitespaces),
                         workHosts: Settings.splitList(workHosts.stringValue),
                         privateWindows: windows,
                         privateApps: Settings.splitList(privateApps.stringValue))
        let empty = s.validationErrors()
        if !empty.isEmpty {
            alert("Не заполнены обязательные поля", empty.joined(separator: "\n"))
            return
        }
        do {
            try SettingsStore.save(s)
        } catch {
            alert("Настройки не сохранены", error.localizedDescription)
            return
        }
        Log.shared.info("settings saved")
        close()
    }

    private func alert(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.alertStyle = .warning
        if let window { a.beginSheetModal(for: window) } else { a.runModal() }
    }
}
