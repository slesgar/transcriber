import AppKit
import ApplicationServices
import Foundation

public struct MeetingInfo: Equatable {
    public var title: String
    public var participants: [String]
    public init(title: String = "", participants: [String] = []) {
        self.title = title
        self.participants = participants
    }
}

public enum TalkInspector {
    public static let bundleID = "kontur.talk"
    private static let namePattern = try! NSRegularExpression(
        pattern: #"^\p{Lu}[\p{L}'-]+(?: \p{Lu}[\p{L}'-]+){1,2}$"#)
    /// Trailing app-name suffix of a Talk window title: «Синк — Толк», or the bare «Толк».
    private static let talkSuffixPattern = try! NSRegularExpression(pattern: #"\s*[—–-]?\s*Толк\s*$"#)

    /// Prompts for Accessibility permission once (system dialog) and reports the current state.
    public static func ensureAccessibility() -> Bool {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        return AXIsProcessTrustedWithOptions(opts)
    }

    public static func inspect() -> MeetingInfo {
        guard AXIsProcessTrusted() else {
            Log.shared.warn("talk inspector: accessibility not granted")
            return MeetingInfo()
        }
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first else {
            return MeetingInfo()
        }
        let element = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetAttributeValue(element, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        var windowsValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &windowsValue) == .success,
              let windows = windowsValue as? [AXUIElement], let window = windows.first else {
            Log.shared.warn("talk inspector: no windows")
            return MeetingInfo()
        }
        let title = cleanTitle(stringAttribute(window, kAXTitleAttribute))
        var texts: [String] = []
        var visited = 0
        collectStaticTexts(window, into: &texts, depth: 0, visited: &visited)
        let names = participantNames(from: texts)
        Log.shared.info("talk inspector: title '\(title)', \(texts.count) texts, names \(names)")
        return MeetingInfo(title: title, participants: names)
    }

    /// Strips only the trailing «— Толк» app suffix, so a meeting actually named «Толк про роадмап» survives.
    public static func cleanTitle(_ raw: String) -> String {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let stripped = talkSuffixPattern.stringByReplacingMatches(
            in: t, range: NSRange(t.startIndex..., in: t), withTemplate: "")
        return stripped.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Keeps strings that look like a person's name (2–3 capitalised words), de-duplicated in order.
    public static func participantNames(from texts: [String]) -> [String] {
        var seen = Set<String>()
        return texts.compactMap { raw in
            let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            let range = NSRange(t.startIndex..., in: t)
            guard namePattern.firstMatch(in: t, range: range) != nil, !seen.contains(t) else { return nil }
            seen.insert(t)
            return t
        }
    }

    private static func stringAttribute(_ element: AXUIElement, _ name: String) -> String {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return "" }
        return value as? String ?? ""
    }

    /// `visited` caps the total node count: a pathological AX tree must not hang the start of a recording.
    private static func collectStaticTexts(_ element: AXUIElement, into out: inout [String],
                                           depth: Int, visited: inout Int) {
        guard depth < 40, out.count < 2000, visited < 5000 else { return }
        visited += 1
        let role = stringAttribute(element, kAXRoleAttribute)
        if role == kAXStaticTextRole {
            let v = stringAttribute(element, kAXValueAttribute)
            if !v.isEmpty { out.append(v) }
        }
        var childrenValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &childrenValue) == .success,
              let children = childrenValue as? [AXUIElement] else { return }
        for child in children { collectStaticTexts(child, into: &out, depth: depth + 1, visited: &visited) }
    }
}
