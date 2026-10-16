import Foundation

/// What the owner edits in «Настройки…»: who they are and where notes go (the worker's
/// `config.yaml`) plus the privacy rules (the app's `app-config.json`). Everything else in both
/// files stays as it is.
public struct Settings: Equatable {
    public var ownerName: String
    public var ownerTalkName: String
    public var vaultRoot: String
    public var privateDir: String
    public var workHosts: [String]
    public var privateWindows: [PrivateWindow]
    public var privateApps: [String]

    public init(ownerName: String, ownerTalkName: String, vaultRoot: String, privateDir: String,
                workHosts: [String], privateWindows: [PrivateWindow], privateApps: [String]) {
        self.ownerName = ownerName
        self.ownerTalkName = ownerTalkName
        self.vaultRoot = vaultRoot
        self.privateDir = privateDir
        self.workHosts = workHosts
        self.privateWindows = privateWindows
        self.privateApps = privateApps
    }

    /// Russian names of the required fields left empty: without an owner name the notes have no
    /// owner to attribute lines to, and an empty path would send transcripts — private ones
    /// included — to a relative directory.
    public func validationErrors() -> [String] {
        [("Имя владельца", ownerName), ("Вольт (путь)", vaultRoot), ("Приватные записи (путь)", privateDir)]
            .filter { $0.1.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            .map(\.0)
    }

    /// A host or bundle-id list typed into one field: commas and any whitespace separate items.
    public static func splitList(_ text: String) -> [String] {
        text.split(whereSeparator: { $0 == "," || $0.isWhitespace }).map(String.init)
    }
}

public enum SettingsError: LocalizedError {
    case appConfigNotJSON(URL)
    case privacyMalformed(URL)

    public var errorDescription: String? {
        switch self {
        case .appConfigNotJSON(let url):
            return "\(url.path) — не JSON-объект. Исправьте файл вручную, настройки не сохранены."
        case .privacyMalformed(let url):
            return "\(url.path): ключ privacy не читается как правила приватности. Исправьте файл вручную, настройки не сохранены."
        }
    }
}

/// Reads and writes the two config files without rewriting them: `config.yaml` is edited line by
/// line with the same contract as the worker's `firstrun.set_yaml_keys` (a `^key:` line is
/// replaced, comments and other keys stay in place, missing keys are appended, strings are
/// double-quoted with JSON escaping), `app-config.json` only gets three fields of `privacy`
/// replaced. The worker re-reads `config.yaml` when the next session starts (воркер перечитывает
/// конфиг при старте следующей сессии), the app reads `app-config.json` afresh, so writing is
/// enough. `owner_thoughts_heading` is never written: the vault looks the heading up verbatim,
/// so it is edited in `config.yaml` only, like the thresholds.
public enum SettingsStore {
    static let defaultOwnerName = "Owner"
    static let defaultOwnerTalkName = ""
    static let defaultVaultRoot = "~/Documents/Vault"
    static let defaultPrivateDir = "~/Documents/Приватные записи"

    public static func load(appConfig: URL = AppPaths.appConfig,
                            workerConfig: URL = AppPaths.workerConfig) -> Settings {
        let rules = PrivacyRules.load(from: appConfig)
        let yaml = (try? String(contentsOf: workerConfig, encoding: .utf8)) ?? ""
        return Settings(
            ownerName: yamlValue(yaml, key: "owner_name") ?? defaultOwnerName,
            ownerTalkName: yamlValue(yaml, key: "owner_talk_name") ?? defaultOwnerTalkName,
            vaultRoot: yamlValue(yaml, key: "vault_root") ?? defaultVaultRoot,
            privateDir: yamlValue(yaml, key: "private_dir") ?? defaultPrivateDir,
            workHosts: rules.workHosts,
            privateWindows: rules.windows,
            privateApps: rules.apps)
    }

    /// Creates the data directory if needed; each file is written atomically. A broken
    /// `app-config.json` is an error rather than something to overwrite: it may hold the owner's
    /// `talk`/`recon` tuning. So is a `privacy` value that does not decode: `load` showed the
    /// defaults for it, and saving those would wipe the owner's schedule.
    public static func save(_ s: Settings, appConfig: URL = AppPaths.appConfig,
                            workerConfig: URL = AppPaths.workerConfig) throws {
        let fm = FileManager.default
        for dir in Set([appConfig.deletingLastPathComponent(), workerConfig.deletingLastPathComponent()]) {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        if !fm.fileExists(atPath: appConfig.path) {
            _ = PrivacyRules.load(from: appConfig)   // writes the defaults with their `_comment`
        }
        let json = (try? String(contentsOf: appConfig, encoding: .utf8)) ?? "{}"
        if privacyIsMalformed(json) { throw SettingsError.privacyMalformed(appConfig) }
        guard let newJSON = settingPrivacy(json, windows: s.privateWindows, apps: s.privateApps,
                                           workHosts: s.workHosts) else {
            throw SettingsError.appConfigNotJSON(appConfig)
        }
        let yaml = (try? String(contentsOf: workerConfig, encoding: .utf8)) ?? ""
        let newYAML = settingYAML(yaml, values: [
            ("owner_name", s.ownerName),
            ("owner_talk_name", s.ownerTalkName),
            ("vault_root", s.vaultRoot),
            ("private_dir", s.privateDir),
        ])
        try newJSON.write(to: appConfig, atomically: true, encoding: .utf8)
        try newYAML.write(to: workerConfig, atomically: true, encoding: .utf8)
    }

    // MARK: - config.yaml

    public static func settingYAML(_ text: String, values: [(key: String, value: String)]) -> String {
        var text = text
        for (key, value) in values {
            let line = "\(key): \(quoted(value))"
            if let range = keyLineRange(text, key: key) {
                text.replaceSubrange(range, with: line)
            } else {
                text += (text.isEmpty || text.hasSuffix("\n") ? "" : "\n") + line + "\n"
            }
        }
        return text
    }

    /// The whole first `^key:` line, without its newline; a key inside a comment never matches.
    private static func keyLineRange(_ text: String, key: String) -> Range<String.Index>? {
        let pattern = "^" + NSRegularExpression.escapedPattern(for: key) + ":.*$"
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.anchorsMatchLines]),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) else { return nil }
        return Range(match.range, in: text)
    }

    /// A string the way Python's `json.dumps(…, ensure_ascii=False)` writes it — valid YAML too.
    private static func quoted(_ value: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: value,
                                                     options: [.fragmentsAllowed, .withoutEscapingSlashes]),
              let text = String(data: data, encoding: .utf8) else { return "\"\"" }
        return text
    }

    /// The scalar after `key:` — a double-quoted string is JSON-unescaped, a single-quoted one
    /// loses its quotes, a plain one loses a trailing `# comment`. Nil when the key is absent.
    static func yamlValue(_ text: String, key: String) -> String? {
        guard let range = keyLineRange(text, key: key) else { return nil }
        let raw = text[range].dropFirst(key.count + 1).trimmingCharacters(in: .whitespaces)
        if raw.hasPrefix("\"") {
            var escaped = false
            for i in raw.indices.dropFirst() {
                let c = raw[i]
                if escaped { escaped = false; continue }
                if c == "\\" { escaped = true; continue }
                if c == "\"" {
                    let literal = String(raw[...i])
                    if let data = literal.data(using: .utf8),
                       let s = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) as? String {
                        return s
                    }
                    break
                }
            }
            return String(raw.dropFirst())
        }
        if raw.hasPrefix("'") {
            let body = raw.dropFirst()
            var out = ""
            var i = body.startIndex
            while i < body.endIndex {
                if body[i] == "'" {
                    let next = body.index(after: i)
                    if next < body.endIndex, body[next] == "'" { out.append("'"); i = body.index(after: next); continue }
                    break
                }
                out.append(body[i]); i = body.index(after: i)
            }
            return out
        }
        var plain = raw
        if let hash = plain.range(of: " #") { plain = String(plain[..<hash.lowerBound]) }
        return plain.trimmingCharacters(in: .whitespaces)
    }

    // MARK: - app-config.json

    /// True when the text is a JSON object whose `privacy` value is present but is not what
    /// `PrivacyRules` decodes. Not-JSON is `settingPrivacy`'s concern (it returns nil).
    static func privacyIsMalformed(_ text: String) -> Bool {
        guard let data = text.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let privacy = object["privacy"] else { return false }
        guard let sub = try? JSONSerialization.data(withJSONObject: privacy, options: [.fragmentsAllowed]),
              (try? JSONDecoder().decode(PrivacyRules.self, from: sub)) != nil else { return true }
        return false
    }

    public static func settingPrivacy(_ text: String, windows: [PrivateWindow], apps: [String],
                                      workHosts: [String]) -> String? {
        guard let data = text.data(using: .utf8),
              var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
        var privacy = object["privacy"] as? [String: Any] ?? [:]
        privacy["windows"] = windows.map { ["weekday": $0.weekday, "from": $0.from, "to": $0.to] as [String: Any] }
        privacy["apps"] = apps
        privacy["workHosts"] = workHosts
        object["privacy"] = privacy
        guard let out = try? JSONSerialization.data(withJSONObject: object,
                                                    options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
              let string = String(data: out, encoding: .utf8) else { return nil }
        return pythonStyleKeys(string) + "\n"
    }

    /// `.prettyPrinted` writes `"key" : value`, the worker's `json.dumps(indent=2)` writes
    /// `"key": value`; without this the file would churn between the two writers. Only a key at
    /// the start of a line is touched, never a string value that happens to contain `" : `.
    static func pythonStyleKeys(_ text: String) -> String {
        guard let regex = try? NSRegularExpression(pattern: #"^(\s*"(?:[^"\\]|\\.)*") : "#,
                                                   options: [.anchorsMatchLines]) else { return text }
        return regex.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text),
                                              withTemplate: "$1: ")
    }
}
