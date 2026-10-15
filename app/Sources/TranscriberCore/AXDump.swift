import AppKit
import ApplicationServices
import Foundation

/// One node of an Accessibility tree, flattened to the attributes worth looking at during
/// reconnaissance. Kept separate from the walking code so the line format is testable.
public struct AXNodeRecord: Equatable {
    public var role = ""
    public var subrole = ""
    public var title = ""
    public var desc = ""
    public var value = ""
    public var help = ""
    public var domClass = ""
    public var domIdentifier = ""
    public var selected: Bool?
    public var focused: Bool?
    public var frame: CGRect?

    public init() {}

    /// Values can be whole paragraphs of chat; only the head of one is useful in a dump.
    public static let valueLimit = 80

    public static func cut(_ s: String, limit: Int = valueLimit) -> String {
        let flat = s.replacingOccurrences(of: "\n", with: "⏎")
        guard flat.count > limit else { return flat }
        return String(flat.prefix(limit)) + "…"
    }

    /// `depth` spaces of indentation, then only the attributes that are actually present.
    public func line(depth: Int) -> String {
        var parts: [String] = []
        func add(_ name: String, _ raw: String) {
            let v = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !v.isEmpty else { return }
            parts.append("\(name)='\(AXNodeRecord.cut(v))'")
        }
        parts.append(role.isEmpty ? "?" : role)
        add("subrole", subrole)
        add("title", title)
        add("desc", desc)
        add("value", value)
        add("help", help)
        add("class", domClass)
        add("id", domIdentifier)
        if let selected { parts.append("selected=\(selected)") }
        if let focused { parts.append("focused=\(focused)") }
        if let frame {
            parts.append(String(format: "frame=%.0f,%.0f %.0fx%.0f",
                                frame.origin.x, frame.origin.y, frame.size.width, frame.size.height))
        }
        return String(repeating: "  ", count: max(depth, 0)) + parts.joined(separator: " ")
    }
}

/// Read-only reconnaissance over another app's Accessibility tree: no clicks, no writes beyond
/// the two flags that ask Electron/Chrome to expose their web content at all.
public enum AXDump {
    /// Electron apps build the AX tree only once someone asks for it; Chrome needs the second flag
    /// to expose web content. Both are the standard opt-in that screen readers set.
    public static func enableEnhancedAccessibility(_ app: AXUIElement) {
        AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
    }

    /// First running app whose bundle id starts with `bundlePrefix`; an exact match wins over a
    /// helper process («kontur.talk» vs «kontur.talk.helper.renderer»).
    public static func pidOf(bundlePrefix: String) -> pid_t? {
        let apps = NSWorkspace.shared.runningApplications.filter {
            ($0.bundleIdentifier ?? "").hasPrefix(bundlePrefix)
        }
        if let exact = apps.first(where: { $0.bundleIdentifier == bundlePrefix }) {
            return exact.processIdentifier
        }
        return apps.first?.processIdentifier
    }

    /// Depth-first text dump of every window of `pid`. Returns a header plus one line per node;
    /// never throws and never crashes on a missing attribute — an app under reconnaissance is
    /// allowed to answer nothing.
    public static func dump(pid: pid_t, maxNodes: Int = 20_000, maxDepth: Int = 60) -> String {
        guard AXIsProcessTrusted() else { return "accessibility not granted" }
        let app = AXUIElementCreateApplication(pid)
        enableEnhancedAccessibility(app)
        var out: [String] = []
        var visited = 0
        let windows = children(of: app, attribute: kAXWindowsAttribute as String)
        let roots = windows.isEmpty ? children(of: app, attribute: kAXChildrenAttribute as String) : windows
        out.append("pid \(pid), windows \(windows.count), roots \(roots.count)")
        for (i, root) in roots.enumerated() {
            out.append("--- root \(i)")
            walk(root, depth: 0, out: &out, visited: &visited, maxNodes: maxNodes, maxDepth: maxDepth)
        }
        out.append("--- nodes \(visited)")
        return out.joined(separator: "\n")
    }

    /// Everything about one node, with every AX call guarded.
    public static func record(of element: AXUIElement) -> AXNodeRecord {
        var r = AXNodeRecord()
        r.role = string(element, kAXRoleAttribute as String)
        r.subrole = string(element, kAXSubroleAttribute as String)
        r.title = string(element, kAXTitleAttribute as String)
        r.desc = string(element, kAXDescriptionAttribute as String)
        r.value = string(element, kAXValueAttribute as String)
        r.help = string(element, kAXHelpAttribute as String)
        r.domClass = domClassList(element)
        r.domIdentifier = string(element, "AXDOMIdentifier")
        r.selected = bool(element, "AXSelected")
        r.focused = bool(element, "AXFocused")
        r.frame = frame(of: element)
        return r
    }

    private static func walk(_ element: AXUIElement, depth: Int, out: inout [String],
                             visited: inout Int, maxNodes: Int, maxDepth: Int) {
        guard visited < maxNodes, depth <= maxDepth else { return }
        visited += 1
        out.append(record(of: element).line(depth: depth))
        for child in children(of: element, attribute: kAXChildrenAttribute as String) {
            walk(child, depth: depth + 1, out: &out, visited: &visited, maxNodes: maxNodes, maxDepth: maxDepth)
        }
    }

    private static func children(of element: AXUIElement, attribute: String) -> [AXUIElement] {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
              let items = value as? [AXUIElement] else { return [] }
        return items
    }

    /// Strings only: an AXValue of a slider or a bounds struct is noise in a text dump.
    private static func string(_ element: AXUIElement, _ name: String) -> String {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return "" }
        if let s = value as? String { return s }
        if let n = value as? NSNumber, name == kAXValueAttribute as String { return n.stringValue }
        return ""
    }

    private static func bool(_ element: AXUIElement, _ name: String) -> Bool? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success,
              let n = value as? NSNumber else { return nil }
        return n.boolValue
    }

    /// Chrome and Electron publish web classes as an array of strings — the closest thing to the
    /// DOM we can see from outside, and where a «speaking» marker would show up.
    private static func domClassList(_ element: AXUIElement) -> String {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, "AXDOMClassList" as CFString, &value) == .success else { return "" }
        if let list = value as? [String] { return list.joined(separator: " ") }
        return value as? String ?? ""
    }

    private static func frame(of element: AXUIElement) -> CGRect? {
        var positionValue: CFTypeRef?
        var sizeValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &positionValue) == .success,
              AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeValue) == .success else { return nil }
        // Both are AXValue boxes; `as!` on a foreign type would trap, so check before unwrapping.
        guard CFGetTypeID(positionValue!) == AXValueGetTypeID(), CFGetTypeID(sizeValue!) == AXValueGetTypeID() else {
            return nil
        }
        var point = CGPoint.zero
        var size = CGSize.zero
        guard AXValueGetValue(positionValue as! AXValue, .cgPoint, &point),
              AXValueGetValue(sizeValue as! AXValue, .cgSize, &size) else { return nil }
        return CGRect(origin: point, size: size)
    }
}
