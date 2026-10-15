import AppKit
import Foundation

public enum Obsidian {
    public static func openURL(path: String) -> URL {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        let encoded = path.addingPercentEncoding(withAllowedCharacters: allowed) ?? path
        return URL(string: "obsidian://open?path=\(encoded)")!
    }

    /// Opens the note in Obsidian (launches it if needed). Returns false when the path is unknown.
    @discardableResult
    public static func open(path: String?) -> Bool {
        guard let path, !path.isEmpty else { return false }
        NSWorkspace.shared.open(openURL(path: path))
        return true
    }
}
