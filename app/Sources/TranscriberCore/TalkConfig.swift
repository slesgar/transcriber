import Foundation

/// Whether the app watches the Толк grid for the whole of a non-private recording and writes
/// `talk.jsonl` into the session. Lives in the same `app-config.json` as `PrivacyRules` and
/// `ReconConfig`, under its own `talk` key — one file the owner edits, three independent readers.
///
/// Like recon, this runs only for a **non-private** session: the participants of a private call
/// have no business being written down anywhere.
public struct TalkConfig: Codable, Equatable {
    public var enabled: Bool

    public init(enabled: Bool) {
        self.enabled = enabled
    }

    public static let defaults = TalkConfig(enabled: true)

    /// The key is optional: a half-written block means «this one thing», not «throw the file away».
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try c.decodeIfPresent(Bool.self, forKey: .enabled) ?? TalkConfig.defaults.enabled
    }

    // MARK: - Config file

    /// The `talk` key of the JSON at `url`. A file that has no such key gets the defaults written
    /// into it (so there is something to switch off), a broken block is ignored, and a missing file
    /// is left alone — `PrivacyRules.load` owns creating it.
    public static func load(from url: URL = PrivacyRules.defaultURL) -> TalkConfig {
        guard let data = try? Data(contentsOf: url), let text = String(data: data, encoding: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return defaults
        }
        guard let talk = object["talk"] else {
            addDefaults(to: url, text: text)
            return defaults
        }
        guard let sub = try? JSONSerialization.data(withJSONObject: talk),
              let config = try? JSONDecoder().decode(TalkConfig.self, from: sub) else {
            Log.shared.warn("talk config: 'talk' in \(url.path) is malformed, using defaults")
            return defaults
        }
        return config
    }

    /// Adds the `talk` block **textually**, the way `ReconConfig` does: every existing byte of the
    /// file stays where it is, and a splice that did not produce valid JSON is dropped.
    static func addDefaults(to url: URL, text: String) {
        guard let spliced = ReconConfig.insertingLastMember(fragment(defaults), into: text),
              let data = spliced.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["talk"] != nil else {
            Log.shared.warn("talk config: could not add the 'talk' block to \(url.path)")
            return
        }
        do {
            try data.write(to: url, options: .atomic)
            Log.shared.info("talk config: 'talk' block added to \(url.path)")
        } catch {
            Log.shared.warn("talk config: \(url.path) not writable: \(error)")
        }
    }

    /// The block as it should read in the file, indented one level.
    static func fragment(_ config: TalkConfig, indent: String = "  ") -> String {
        let comment = "наблюдение за сеткой участников Толка на протяжении всей НЕприватной "
            + "записи: кто в звонке, у кого включён микрофон и кто говорит, строкой JSON на "
            + "каждое изменение в talk.jsonl рядом со звуком сессии. enabled: false выключает."
        return """
        \(indent)"talk": {
        \(indent)\(indent)"_comment": "\(comment)",
        \(indent)\(indent)"enabled": \(config.enabled)
        \(indent)}
        """
    }
}
