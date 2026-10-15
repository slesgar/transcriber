import Foundation

/// How long, and whether at all, the app watches the Accessibility tree of a meeting app at the
/// start of a recording. Lives in the same `app-config.json` as `PrivacyRules`, under its own
/// `recon` key — one file the owner edits, two independent readers.
///
/// Recon runs only for a **non-private** session: the whole point is to learn how Толк marks the
/// speaker, and a private call has no business leaving an AX dump on disk.
public struct ReconConfig: Codable, Equatable {
    public var enabled: Bool
    /// Seconds of sampling from the start of a session. Clamped on load: a typo must not leave the
    /// app dumping an AX tree all day.
    public var seconds: Double

    public init(enabled: Bool, seconds: Double) {
        self.enabled = enabled
        self.seconds = seconds
    }

    public static let defaults = ReconConfig(enabled: true, seconds: 180)
    public static let maxSeconds: Double = 3600

    /// Both keys are optional: a half-written block means «this one thing», not «throw the file
    /// away». Unknown keys (`_comment`) are ignored by `Codable` on their own.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try c.decodeIfPresent(Bool.self, forKey: .enabled) ?? ReconConfig.defaults.enabled
        let raw = try c.decodeIfPresent(Double.self, forKey: .seconds) ?? ReconConfig.defaults.seconds
        seconds = min(max(raw, 0), ReconConfig.maxSeconds)
    }

    // MARK: - Config file

    /// The `recon` key of the JSON at `url`. A file that has no such key gets the defaults written
    /// into it (so there is something to edit) and the defaults returned; a broken block is ignored.
    /// A missing file is left alone — `PrivacyRules.load` owns creating it, and two writers racing
    /// to create the same file is how a config loses half its keys.
    public static func load(from url: URL = PrivacyRules.defaultURL) -> ReconConfig {
        guard let data = try? Data(contentsOf: url), let text = String(data: data, encoding: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return defaults
        }
        guard let recon = object["recon"] else {
            addDefaults(to: url, text: text)
            return defaults
        }
        guard let sub = try? JSONSerialization.data(withJSONObject: recon),
              let config = try? JSONDecoder().decode(ReconConfig.self, from: sub) else {
            Log.shared.warn("recon config: 'recon' in \(url.path) is malformed, using defaults")
            return defaults
        }
        return config
    }

    /// Adds the `recon` block to a config that has none, **textually**: every existing byte stays
    /// where it is. Re-serialising the parsed object would reorder and reformat the `privacy` block
    /// that the owner maintains by hand, which is a diff nobody asked for. The result is parsed
    /// back before it is written — a splice that did not produce valid JSON is dropped.
    static func addDefaults(to url: URL, text: String) {
        guard let spliced = insertingLastMember(fragment(defaults), into: text),
              let data = spliced.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["recon"] != nil else {
            Log.shared.warn("recon config: could not add the 'recon' block to \(url.path)")
            return
        }
        do {
            try data.write(to: url, options: .atomic)
            Log.shared.info("recon config: 'recon' block added to \(url.path)")
        } catch {
            Log.shared.warn("recon config: \(url.path) not writable: \(error)")
        }
    }

    /// The block as it should read in the file, indented one level — written by hand rather than
    /// by an encoder so the style matches the rest of the file exactly.
    static func fragment(_ config: ReconConfig, indent: String = "  ") -> String {
        let seconds = config.seconds == config.seconds.rounded()
            ? String(Int(config.seconds)) : String(config.seconds)
        let comment = "разведка Accessibility-дерева приложения встречи в первые seconds секунд "
            + "НЕприватной записи: снимки, лог изменений и сводка ложатся в "
            + "~/Library/Logs/transcriber-ax/<id сессии>/. enabled: false выключает целиком."
        return """
        \(indent)"recon": {
        \(indent)\(indent)"_comment": "\(comment)",
        \(indent)\(indent)"enabled": \(config.enabled),
        \(indent)\(indent)"seconds": \(seconds)
        \(indent)}
        """
    }

    /// Splices `fragment` in as the last member of the JSON object in `text`. nil when `text` does
    /// not end in a closing brace, which is the only shape this can handle safely.
    static func insertingLastMember(_ fragment: String, into text: String) -> String? {
        guard let close = text.lastIndex(of: "}") else { return nil }
        let head = text[text.startIndex..<close]
        let tail = text[text.index(after: close)...]
        guard let lastFilled = head.lastIndex(where: { !$0.isWhitespace }) else { return nil }
        let body = head[head.startIndex...lastFilled]
        let comma = body.last == "{" ? "" : ","
        let rest = tail.trimmingCharacters(in: .whitespacesAndNewlines)
        return body + comma + "\n" + fragment + "\n}" + rest + "\n"
    }
}
