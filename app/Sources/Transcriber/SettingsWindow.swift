import AppKit
import TranscriberCore

/// «Настройки…»: five sections on a toolbar (Владелец · Куда пишет · Запись · Приватность · Агент).
/// Every section carries the same «Отмена / Сохранить»: saving takes the whole window and writes
/// `config.yaml` and `app-config.json` through `SettingsStore`; the worker re-reads its config when
/// the next session starts (воркер перечитывает конфиг при старте следующей сессии), the app at its
/// next read, so nothing has to be restarted. Thresholds and the owner-thoughts heading stay in the
/// files: the first are tuned by the quality bench, the second is looked up verbatim by the vault.
final class SettingsWindow: NSWindowController, NSTextFieldDelegate {
    static let shared = SettingsWindow()

    // Владелец
    private let ownerName = NSTextField()
    private let ownerTalkName = NSTextField()
    // Куда пишет
    private let vaultRoot = NSTextField()
    private let rawSubdir = NSTextField()
    private let meetingsSubdir = NSTextField()
    private let privateDir = NSTextField()
    private let vaultStatus = SettingsWindow.statusLabel()
    private let rawStatus = SettingsWindow.statusLabel()
    private let meetingsStatus = SettingsWindow.statusLabel()
    private let privateStatus = SettingsWindow.statusLabel()
    // Запись
    private let workHosts = NSTextField()
    private let talkEnabled = NSButton(checkboxWithTitle: "Следить за участниками Толка", target: nil, action: nil)
    private let keepAudio = NSButton(checkboxWithTitle: "Хранить аудио после распознавания", target: nil, action: nil)
    // Приватность
    private let privateApps = NSTextField()
    private let windowRows = NSStackView()
    /// Windows from the file the rows cannot show (a weekday outside 1…7, an unreadable time):
    /// saved back untouched, so opening and saving the window never turns a dead rule into a live one.
    private var unreadableWindows: [PrivateWindow] = []
    private let unreadableNote = NSTextField(wrappingLabelWithString: "")
    // Агент
    private let agentCommand = NSTextField()
    /// Agents found on the owner's PATH, then «Своя команда…»; picking one fills the command.
    private let agentPicker = NSPopUpButton()
    private var presets: [AgentPreset] = []
    private static let customTitle = "Своя команда…"
    private let contextDir = NSTextField()
    private let contextStatus = SettingsWindow.statusLabel()
    private let timeout = NSTextField()
    private let timeoutStepper = NSStepper()
    private let checkResult = NSTextField(wrappingLabelWithString: "")
    private let checkButton = NSButton(title: "Проверить", target: nil, action: nil)

    private let tabs = NSTabViewController()
    static let size = NSSize(width: 640, height: 470)

    private init() {
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: Self.size),
                              styleMask: [.titled, .closable], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        window.toolbarStyle = .preference
        super.init(window: window)
        build()
        window.contentViewController = tabs
        window.setContentSize(Self.size)
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

    // MARK: - Sections

    private func build() {
        tabs.tabStyle = .toolbar
        tabs.transitionOptions = []
        let sections: [(String, String, NSView)] = [
            ("Владелец", "person.crop.circle", ownerSection()),
            ("Куда пишет", "folder", pathsSection()),
            ("Запись", "mic", recordingSection()),
            ("Приватность", "lock", privacySection()),
            ("Агент", "sparkles", agentSection()),
        ]
        for (title, symbol, content) in sections {
            let vc = NSViewController()
            vc.title = title
            vc.view = pane(content)
            let item = NSTabViewItem(viewController: vc)
            item.label = title
            item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
            tabs.addTabViewItem(item)
        }
    }

    private func ownerSection() -> NSView {
        ownerTalkName.placeholderString = "как подписана твоя плитка в Толке"
        return column([
            group("Кто ведёт записи", rows: [
                row("Имя владельца", ownerName),
                row("Имя в Толке", ownerTalkName,
                    note: "По нему транскрибатор узнаёт твою плитку и не путает тебя с собеседниками."),
            ]),
        ])
    }

    private func pathsSection() -> NSView {
        for field in [vaultRoot, rawSubdir, meetingsSubdir, privateDir] { field.delegate = self }
        let dataDir = NSTextField(labelWithString: AppPaths.dataDir.path)
        dataDir.textColor = .secondaryLabelColor
        dataDir.lineBreakMode = .byTruncatingMiddle
        dataDir.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let reveal = smallButton("Показать в Finder", #selector(revealDataDir))
        reveal.setContentCompressionResistancePriority(.required, for: .horizontal)
        return column([
            group("Рабочие встречи", rows: [
                row("Вольт", folderField(vaultRoot, status: vaultStatus, choose: #selector(chooseVault))),
                row("Транскрипты", insideVault(rawSubdir, status: rawStatus)),
                row("Заметки встреч", insideVault(meetingsSubdir, status: meetingsStatus)),
            ]),
            group("Приватные встречи", rows: [
                row("Папка", folderField(privateDir, status: privateStatus, choose: #selector(choosePrivate)),
                    note: "Вне вольта: в граф и рутины не попадает."),
            ]),
            group("Данные транскрибатора", rows: [
                row("Каталог данных", hstack([dataDir, reveal])),
            ]),
        ])
    }

    private func recordingSection() -> NSView {
        workHosts.placeholderString = "talk.example.com, meet.example.com"
        return column([
            group("Встречи", rows: [
                row("Хосты встреч", workHosts,
                    note: "Звонок в браузере на этих хостах — рабочая встреча; через запятую."),
            ]),
            group("Что сохранять", rows: [
                row("", talkEnabled, note: "Кто в звонке и кто говорит — для имён в транскрипте. В приватной записи не работает."),
                row("", keepAudio, note: "Нужно для перераспознавания и стенда качества; без этого аудио удаляется после встречи."),
            ]),
        ])
    }

    private func privacySection() -> NSView {
        privateApps.placeholderString = "com.example.chat"
        windowRows.orientation = .vertical
        windowRows.alignment = .leading
        windowRows.spacing = 6
        unreadableNote.font = .systemFont(ofSize: 11)
        unreadableNote.textColor = .systemOrange
        unreadableNote.preferredMaxLayoutWidth = 400
        let scroll = NSScrollView()
        scroll.documentView = windowRows
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        windowRows.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            scroll.widthAnchor.constraint(equalToConstant: 400),
            scroll.heightAnchor.constraint(equalToConstant: 132),   // ~4 rows; more scroll
            windowRows.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            windowRows.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            windowRows.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
        ])
        scroll.contentView.postsBoundsChangedNotifications = false
        let add = smallButton("Добавить окно", #selector(addWindowRow))
        add.image = NSImage(systemSymbolName: "plus", accessibilityDescription: nil)
        add.imagePosition = .imageLeading
        return column([
            group("Что всегда приватно", rows: [
                row("Приложения", privateApps, note: "Bundle id через запятую: запись из них уходит в приватную папку."),
            ]),
            group("Окна приватности", rows: [
                row("", vstack([scroll, add, unreadableNote], spacing: 8),
                    note: "Запись, начатая в это время, уходит в приватную папку. Толк и хосты встреч всегда рабочие."),
            ]),
        ])
    }

    private func agentSection() -> NSView {
        agentCommand.placeholderString = "my-agent --print"
        agentPicker.target = self
        agentPicker.action = #selector(pickAgent)
        rebuildPicker(found: [])
        contextDir.delegate = self
        timeout.formatter = { let f = NumberFormatter(); f.minimum = 1; f.maximum = 3600; f.allowsFloats = false; return f }()
        timeout.alignment = .right
        timeout.translatesAutoresizingMaskIntoConstraints = false
        timeout.widthAnchor.constraint(equalToConstant: 60).isActive = true
        timeoutStepper.minValue = 10
        timeoutStepper.maxValue = 3600
        timeoutStepper.increment = 10
        timeoutStepper.target = self
        timeoutStepper.action = #selector(stepTimeout)
        let seconds = NSTextField(labelWithString: "с")
        seconds.textColor = .secondaryLabelColor
        checkButton.target = self
        checkButton.action = #selector(checkAgent)
        checkButton.bezelStyle = .rounded
        checkResult.font = .systemFont(ofSize: 11)
        checkResult.textColor = .secondaryLabelColor
        checkResult.preferredMaxLayoutWidth = 380
        return column([
            group("Свой агент", rows: [
                row("Агент", agentPicker,
                    note: "Найденные у тебя агенты уже с нужной командой: отвечают один раз и ничего не меняют в файлах."),
                row("Команда", agentCommand,
                    note: "Вопрос с текстом встречи уходит команде на вход, ответ читается с её вывода. Пусто — агент выключен."),
                row("", hstack([checkButton, checkResult])),
            ]),
            group("Ответы", rows: [
                row("Контекст проектов", folderField(contextDir, status: contextStatus, choose: #selector(chooseContext)),
                    note: "С переключателем «контекст проектов» агент запускается из этой папки и видит её файлы."),
                row("Ждать ответа", hstack([timeout, timeoutStepper, seconds])),
            ]),
        ])
    }

    // MARK: - Layout helpers

    /// A section: its groups top-down, then the save bar pinned to the bottom.
    private func pane(_ content: NSView) -> NSView {
        let cancel = NSButton(title: "Отмена", target: self, action: #selector(cancelAction))
        cancel.keyEquivalent = "\u{1b}"
        let save = NSButton(title: "Сохранить", target: self, action: #selector(saveAction))
        save.keyEquivalent = "\r"
        let bar = hstack([NSView(), cancel, save])
        bar.setHuggingPriority(.defaultLow, for: .horizontal)
        let root = NSView(frame: NSRect(origin: .zero, size: Self.size))
        for v in [content, bar] {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
        }
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: root.topAnchor, constant: 18),
            content.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 22),
            content.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -22),
            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 22),
            bar.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -22),
            bar.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -18),
            content.bottomAnchor.constraint(lessThanOrEqualTo: bar.topAnchor, constant: -12),
            root.widthAnchor.constraint(equalToConstant: Self.size.width),
            root.heightAnchor.constraint(equalToConstant: Self.size.height),
        ])
        return root
    }

    private func column(_ groups: [NSView]) -> NSView {
        let s = vstack(groups, spacing: 14)
        for g in groups { g.widthAnchor.constraint(equalTo: s.widthAnchor).isActive = true }
        return s
    }

    /// A caption over a rounded, lightly filled box of label/control rows — the macOS settings look.
    private func group(_ title: String, rows: [[NSView]]) -> NSView {
        let caption = NSTextField(labelWithString: title)
        caption.font = .systemFont(ofSize: 12, weight: .medium)
        caption.textColor = .secondaryLabelColor
        let grid = NSGridView(views: rows)
        grid.column(at: 0).xPlacement = .trailing
        grid.column(at: 0).width = 150
        grid.rowAlignment = .firstBaseline
        grid.columnSpacing = 12
        grid.rowSpacing = 10
        for i in 0..<grid.numberOfRows where (grid.cell(atColumnIndex: 1, rowIndex: i).contentView as? NSStackView)?.orientation == .vertical {
            grid.row(at: i).yPlacement = .top
            grid.row(at: i).rowAlignment = .none
        }
        let box = NSBox()
        box.boxType = .custom
        box.cornerRadius = 10
        box.borderWidth = 0.5
        box.borderColor = .separatorColor
        box.fillColor = .quaternarySystemFill
        box.contentViewMargins = NSSize(width: 14, height: 12)
        box.contentView = grid
        let s = vstack([caption, box], spacing: 6)
        box.widthAnchor.constraint(equalTo: s.widthAnchor).isActive = true
        caption.leadingAnchor.constraint(equalTo: s.leadingAnchor, constant: 4).isActive = true
        return s
    }

    private func row(_ title: String, _ control: NSView, note: String? = nil) -> [NSView] {
        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.textColor = .secondaryLabelColor
        if let field = control as? NSTextField, field.isEditable {
            field.translatesAutoresizingMaskIntoConstraints = false
            field.widthAnchor.constraint(greaterThanOrEqualToConstant: 380).isActive = true
        }
        guard let note else { return [label, control] }
        let hint = NSTextField(wrappingLabelWithString: note)
        hint.font = .systemFont(ofSize: 11)
        hint.textColor = .tertiaryLabelColor
        hint.preferredMaxLayoutWidth = 400
        return [label, vstack([control, hint], spacing: 3)]
    }

    /// A path field with «Выбрать…» and the line telling whether that folder exists.
    private func folderField(_ field: NSTextField, status: NSTextField, choose: Selector) -> NSView {
        field.translatesAutoresizingMaskIntoConstraints = false
        field.widthAnchor.constraint(greaterThanOrEqualToConstant: 300).isActive = true
        return vstack([hstack([field, smallButton("Выбрать…", choose)]), status], spacing: 3)
    }

    /// A folder inside the vault: the «Вольт /» prefix makes the relative path read as one.
    private func insideVault(_ field: NSTextField, status: NSTextField) -> NSView {
        let prefix = NSTextField(labelWithString: "Вольт /")
        prefix.textColor = .tertiaryLabelColor
        field.translatesAutoresizingMaskIntoConstraints = false
        field.widthAnchor.constraint(greaterThanOrEqualToConstant: 320).isActive = true
        return vstack([hstack([prefix, field]), status], spacing: 3)
    }

    private func vstack(_ views: [NSView], spacing: CGFloat) -> NSStackView {
        let s = NSStackView(views: views)
        s.orientation = .vertical
        s.alignment = .leading
        s.spacing = spacing
        return s
    }

    private func hstack(_ views: [NSView]) -> NSStackView {
        let s = NSStackView(views: views)
        s.orientation = .horizontal
        s.alignment = .firstBaseline
        s.spacing = 8
        return s
    }

    private func smallButton(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        b.controlSize = .small
        return b
    }

    private static func statusLabel() -> NSTextField {
        let l = NSTextField(labelWithString: "")
        l.font = .systemFont(ofSize: 11)
        return l
    }

    // MARK: - Folder status

    func controlTextDidChange(_ obj: Notification) { refreshStatuses() }

    private func refreshStatuses() {
        let vault = vaultRoot.stringValue
        show(FolderStatus.check(FolderStatus.resolve(vault, inside: nil)), in: vaultStatus, missing: "папки нет")
        show(FolderStatus.check(FolderStatus.resolve(rawSubdir.stringValue, inside: vault)), in: rawStatus,
             missing: "папки нет — создастся при первой записи")
        show(FolderStatus.check(FolderStatus.resolve(meetingsSubdir.stringValue, inside: vault)), in: meetingsStatus,
             missing: "папки нет — создастся при первой записи")
        show(FolderStatus.check(FolderStatus.resolve(privateDir.stringValue, inside: nil)), in: privateStatus,
             missing: "папки нет — создастся при первой приватной записи")
        let context = contextDir.stringValue
        if context.trimmingCharacters(in: .whitespaces).isEmpty {
            contextStatus.stringValue = "не задана — переключатель контекста в окне встречи будет недоступен"
            contextStatus.textColor = .tertiaryLabelColor
        } else {
            show(FolderStatus.check(FolderStatus.resolve(context, inside: nil)), in: contextStatus, missing: "папки нет")
        }
    }

    private func show(_ status: FolderStatus, in label: NSTextField, missing: String) {
        switch status {
        case .exists: label.stringValue = "✓ папка есть"; label.textColor = .systemGreen
        case .missing: label.stringValue = "⚠︎ \(missing)"; label.textColor = .systemOrange
        case .notFolder: label.stringValue = "✕ по этому пути файл, а не папка"; label.textColor = .systemRed
        case .empty: label.stringValue = "✕ путь не задан"; label.textColor = .systemRed
        }
    }

    // MARK: - Privacy windows

    /// Пн … Вс on screen, the file keeps the worker's numbering (1 = вс … 7 = сб).
    private static let weekdays: [(String, Int)] = [("Пн", 2), ("Вт", 3), ("Ср", 4), ("Чт", 5), ("Пт", 6), ("Сб", 7), ("Вс", 1)]

    private static let hhmm: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(secondsFromGMT: 0)
        f.dateFormat = "HH:mm"
        return f
    }()

    private final class WindowRow: NSStackView {
        let day = NSPopUpButton()
        let from = NSDatePicker()
        let to = NSDatePicker()
        let remove = NSButton()
    }

    private func appendWindowRow(_ w: PrivateWindow) {
        let r = WindowRow()
        r.orientation = .horizontal
        r.spacing = 8
        r.alignment = .centerY
        r.day.addItems(withTitles: Self.weekdays.map(\.0))
        r.day.selectItem(at: Self.weekdays.firstIndex { $0.1 == w.weekday } ?? 0)   // fill() passes valid ones only
        for (picker, value) in [(r.from, w.from), (r.to, w.to)] {
            picker.datePickerStyle = .textFieldAndStepper
            picker.datePickerElements = .hourMinute
            picker.timeZone = TimeZone(secondsFromGMT: 0)
            picker.locale = Locale(identifier: "ru_RU")
            picker.dateValue = Self.hhmm.date(from: value) ?? Self.hhmm.date(from: "12:00")!
        }
        let dash = NSTextField(labelWithString: "—")
        dash.textColor = .secondaryLabelColor
        r.remove.bezelStyle = .inline
        r.remove.isBordered = false
        r.remove.image = NSImage(systemSymbolName: "minus.circle", accessibilityDescription: "Убрать окно")
        r.remove.target = self
        r.remove.action = #selector(removeWindowRow(_:))
        for v in [r.day, r.from, dash, r.to, r.remove] { r.addArrangedSubview(v) }
        windowRows.addArrangedSubview(r)
    }

    @objc private func addWindowRow() { appendWindowRow(PrivateWindow(weekday: 2, from: "12:00", to: "13:00")) }

    @objc private func removeWindowRow(_ sender: NSButton) {
        guard let r = sender.superview as? WindowRow else { return }
        windowRows.removeArrangedSubview(r)
        r.removeFromSuperview()
    }

    private func collectWindows() -> [PrivateWindow] {
        unreadableWindows + windowRows.arrangedSubviews.compactMap { $0 as? WindowRow }.map { r in
            PrivateWindow(weekday: Self.weekdays[max(r.day.indexOfSelectedItem, 0)].1,
                          from: Self.hhmm.string(from: r.from.dateValue), to: Self.hhmm.string(from: r.to.dateValue))
        }
    }

    // MARK: - Fill and save

    private func fill(_ s: Settings) {
        ownerName.stringValue = s.ownerName
        ownerTalkName.stringValue = s.ownerTalkName
        vaultRoot.stringValue = s.vaultRoot
        rawSubdir.stringValue = s.rawSubdir
        meetingsSubdir.stringValue = s.meetingsSubdir
        privateDir.stringValue = s.privateDir
        workHosts.stringValue = s.workHosts.joined(separator: ", ")
        talkEnabled.state = s.talkEnabled ? .on : .off
        keepAudio.state = s.keepAudio ? .on : .off
        privateApps.stringValue = s.privateApps.joined(separator: ", ")
        for v in windowRows.arrangedSubviews { windowRows.removeArrangedSubview(v); v.removeFromSuperview() }
        s.privateWindows.filter(\.isValid).forEach(appendWindowRow)
        unreadableWindows = s.privateWindows.filter { !$0.isValid }
        unreadableNote.stringValue = unreadableWindows.isEmpty ? "" :
            "В файле есть окна, которые не читаются (\(unreadableWindows.map(\.line).joined(separator: "; "))): "
            + "транскрибатор их пропускает, окно сохранит их как есть — поправь в app-config.json."
        unreadableNote.isHidden = unreadableWindows.isEmpty
        agentCommand.stringValue = s.agent.command
        syncPicker()
        // PATH of the owner's shell may take seconds the first time: the list fills in when ready
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let found = AgentPreset.discover(path: ShellPath.discovered)
            DispatchQueue.main.async { self?.rebuildPicker(found: found) }
        }
        contextDir.stringValue = s.agent.contextDir
        timeout.integerValue = s.agent.timeoutS
        timeoutStepper.integerValue = s.agent.timeoutS
        checkResult.stringValue = ""
        refreshStatuses()
    }

    private func collect() -> Settings {
        func trimmed(_ f: NSTextField) -> String { f.stringValue.trimmingCharacters(in: .whitespaces) }
        return Settings(ownerName: trimmed(ownerName), ownerTalkName: trimmed(ownerTalkName),
                        vaultRoot: trimmed(vaultRoot), privateDir: trimmed(privateDir),
                        workHosts: Settings.splitList(workHosts.stringValue),
                        privateWindows: collectWindows(),
                        privateApps: Settings.splitList(privateApps.stringValue),
                        rawSubdir: trimmed(rawSubdir), meetingsSubdir: trimmed(meetingsSubdir),
                        keepAudio: keepAudio.state == .on, talkEnabled: talkEnabled.state == .on,
                        agent: AgentConfig(command: trimmed(agentCommand), contextDir: trimmed(contextDir),
                                           timeoutS: timeout.integerValue))
    }

    @objc private func cancelAction() { close() }

    @objc private func saveAction() {
        let s = collect()
        let errors = s.validationErrors()
        if !errors.isEmpty {
            alert("Не всё заполнено как надо", errors.joined(separator: "\n"))
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

    // MARK: - Actions

    @objc private func chooseVault() { choose(into: vaultRoot) }
    @objc private func choosePrivate() { choose(into: privateDir) }
    @objc private func chooseContext() { choose(into: contextDir) }

    private func choose(into field: NSTextField) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = "Выбрать"
        let current = FolderStatus.resolve(field.stringValue, inside: nil)
        if FolderStatus.check(current) == .exists { panel.directoryURL = URL(fileURLWithPath: current) }
        guard let window else { return }
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            field.stringValue = Self.abbreviatingHome(url.path)
            self?.refreshStatuses()
        }
    }

    /// `~/…` reads better and survives a renamed home folder.
    static func abbreviatingHome(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if path == home { return "~" }
        return path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
    }

    @objc private func revealDataDir() {
        NSWorkspace.shared.activateFileViewerSelecting([AppPaths.dataDir])
    }

    private func rebuildPicker(found: [AgentPreset]) {
        presets = found
        agentPicker.removeAllItems()
        agentPicker.addItems(withTitles: found.map(\.name))
        if !found.isEmpty { agentPicker.menu?.addItem(.separator()) }
        agentPicker.addItem(withTitle: Self.customTitle)
        syncPicker()
    }

    /// The picker follows the command: a preset's exact command selects it, anything else is «своя».
    private func syncPicker() {
        let command = agentCommand.stringValue.trimmingCharacters(in: .whitespaces)
        if let i = presets.firstIndex(where: { $0.command == command }) {
            agentPicker.selectItem(at: i)
            agentCommand.isEditable = false
            agentCommand.textColor = .secondaryLabelColor
        } else {
            if presets.isEmpty == false, command.isEmpty {
                // nothing chosen yet and an agent is installed: offer it rather than an empty field
                agentPicker.selectItem(at: 0)
                agentCommand.stringValue = presets[0].command
                agentCommand.isEditable = false
                agentCommand.textColor = .secondaryLabelColor
                return
            }
            agentPicker.selectItem(withTitle: Self.customTitle)
            agentCommand.isEditable = true
            agentCommand.textColor = .labelColor
        }
    }

    @objc private func pickAgent() {
        let i = agentPicker.indexOfSelectedItem
        if i >= 0, i < presets.count {
            agentCommand.stringValue = presets[i].command
            agentCommand.isEditable = false
            agentCommand.textColor = .secondaryLabelColor
        } else {
            if AgentPreset.matching(agentCommand.stringValue) != nil { agentCommand.stringValue = "" }
            agentCommand.isEditable = true
            agentCommand.textColor = .labelColor
            window?.makeFirstResponder(agentCommand)
        }
        checkResult.stringValue = ""
    }

    /// Opens the window on the «Агент» section — from the meeting window when no agent is set.
    func showAgent() {
        show()
        tabs.selectedTabViewItemIndex = tabs.tabViewItems.count - 1
    }

    @objc private func stepTimeout() { timeout.integerValue = timeoutStepper.integerValue }

    /// Runs the typed command once with a question that carries no meeting data.
    @objc private func checkAgent() {
        let command = agentCommand.stringValue
        checkResult.textColor = .secondaryLabelColor
        checkResult.stringValue = "Спрашиваю агента…"
        checkButton.isEnabled = false
        let started = Date()
        AgentRunner.run(command: command, prompt: "Ответь одним словом: готов.", cwd: nil,
                        timeout: TimeInterval(min(max(timeout.integerValue, 10), 120))) { [weak self] result in
            guard let self else { return }
            self.checkButton.isEnabled = true
            let secs = Int(Date().timeIntervalSince(started).rounded())
            switch result {
            case .answer(let text):
                self.checkResult.textColor = .systemGreen
                self.checkResult.stringValue = "Ответил за \(secs) с: «\(String(text.prefix(80)))»"
            case .failed(let code, let message):
                self.checkResult.textColor = .systemRed
                self.checkResult.stringValue = "Не вышло (код \(code)): \(message.isEmpty ? "без пояснений" : String(message.prefix(160)))"
            case .timedOut:
                self.checkResult.textColor = .systemRed
                self.checkResult.stringValue = "Не ответил за \(secs) с"
            case .cancelled:
                self.checkResult.stringValue = ""
            case .notConfigured:
                self.checkResult.textColor = .systemOrange
                self.checkResult.stringValue = "Сначала впиши команду"
            }
        }
    }

    private func alert(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.alertStyle = .warning
        if let window { a.beginSheetModal(for: window) } else { a.runModal() }
    }

    // MARK: - Snapshot

    /// For `--settings-test <dir>`: every section as `settings-<n>.png`, without screen recording.
    func snapshots(to dir: URL) {
        fill(SettingsStore.load())
        window?.layoutIfNeeded()
        for i in 0..<tabs.tabViewItems.count {
            tabs.selectedTabViewItemIndex = i
            guard let view = window?.contentView?.superview else { continue }
            view.layoutSubtreeIfNeeded()
            guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { continue }
            view.cacheDisplay(in: view.bounds, to: rep)
            try? rep.representation(using: .png, properties: [:])?
                .write(to: dir.appendingPathComponent("settings-\(i + 1).png"))
        }
    }
}
