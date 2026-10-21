import Foundation

/// The block structure of an agent's answer — what Apple's inline markdown parser leaves out:
/// headings, lists, pipe tables, fenced code, quotes, rules. Inline marks (`**bold**`, `code`)
/// stay inside the block text; the window renders them per block.
public enum MarkdownBlock: Equatable {
    case heading(level: Int, text: String)
    case paragraph(String)
    /// `marker` is "•" for a bullet or "3." for a numbered item; `depth` counts nesting from 0.
    case listItem(marker: String, depth: Int, text: String)
    case table(header: [String], alignments: [Alignment], rows: [[String]])
    case code(String)
    case quote(String)
    case rule

    public enum Alignment: Equatable { case left, center, right }
}

public enum MarkdownBlocks {
    public static func parse(_ text: String) -> [MarkdownBlock] {
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var i = 0

        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: " "))) }
            paragraph = []
        }

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty { flush(); i += 1; continue }

            if trimmed.hasPrefix("```") {   // fenced code: everything up to the closing fence
                flush()
                var code: [String] = []
                i += 1
                while i < lines.count, !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    code.append(lines[i]); i += 1
                }
                blocks.append(.code(code.joined(separator: "\n")))
                i += 1
                continue
            }

            if let (level, rest) = heading(trimmed) { flush(); blocks.append(.heading(level: level, text: rest)); i += 1; continue }

            if isRule(trimmed) { flush(); blocks.append(.rule); i += 1; continue }

            if trimmed.hasPrefix("|"), i + 1 < lines.count, let aligns = separator(lines[i + 1]) {
                flush()
                let header = cells(trimmed)
                var rows: [[String]] = []
                i += 2
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("|") {
                    var row = cells(lines[i].trimmingCharacters(in: .whitespaces))
                    if row.count < header.count { row += Array(repeating: "", count: header.count - row.count) }
                    rows.append(Array(row.prefix(header.count)))
                    i += 1
                }
                let a = aligns.count >= header.count ? Array(aligns.prefix(header.count))
                    : aligns + Array(repeating: .left, count: header.count - aligns.count)
                blocks.append(.table(header: header, alignments: a, rows: rows))
                continue
            }

            if let item = listItem(line) { flush(); blocks.append(item); i += 1; continue }

            if trimmed.hasPrefix(">") {
                flush()
                var quote: [String] = []
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    quote.append(String(lines[i].trimmingCharacters(in: .whitespaces).dropFirst()).trimmingCharacters(in: .whitespaces))
                    i += 1
                }
                blocks.append(.quote(quote.joined(separator: " ")))
                continue
            }

            paragraph.append(trimmed)
            i += 1
        }
        flush()
        return blocks
    }

    static func heading(_ s: String) -> (Int, String)? {
        let hashes = s.prefix(while: { $0 == "#" }).count
        guard (1...6).contains(hashes), s.dropFirst(hashes).first == " " else { return nil }
        return (hashes, String(s.dropFirst(hashes + 1)).trimmingCharacters(in: .whitespaces))
    }

    static func isRule(_ s: String) -> Bool {
        let compact = s.replacingOccurrences(of: " ", with: "")
        guard compact.count >= 3, let c = compact.first, "-*_".contains(c) else { return false }
        return compact.allSatisfy { $0 == c }
    }

    /// `| a | b |` → ["a", "b"]; a `\|` inside a cell stays a pipe.
    static func cells(_ s: String) -> [String] {
        var body = s
        if body.hasPrefix("|") { body.removeFirst() }
        if body.hasSuffix("|") && !body.hasSuffix("\\|") { body.removeLast() }
        let marker = "\u{0}"
        return body.replacingOccurrences(of: "\\|", with: marker).components(separatedBy: "|")
            .map { $0.replacingOccurrences(of: marker, with: "|").trimmingCharacters(in: .whitespaces) }
    }

    /// `|---|:--:|--:|` → alignments; anything else is not a separator row.
    static func separator(_ line: String) -> [MarkdownBlock.Alignment]? {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard t.contains("-"), t.hasPrefix("|") || t.contains("|") else { return nil }
        var result: [MarkdownBlock.Alignment] = []
        for cell in cells(t) {
            guard !cell.isEmpty, cell.allSatisfy({ "-:".contains($0) }), cell.contains("-") else { return nil }
            switch (cell.hasPrefix(":"), cell.hasSuffix(":")) {
            case (true, true): result.append(.center)
            case (false, true): result.append(.right)
            default: result.append(.left)
            }
        }
        return result.isEmpty ? nil : result
    }

    static func listItem(_ line: String) -> MarkdownBlock? {
        let indent = line.prefix(while: { $0 == " " || $0 == "\t" })
            .reduce(0) { $0 + ($1 == "\t" ? 4 : 1) }
        let s = line.trimmingCharacters(in: .whitespaces)
        let depth = indent / 2
        for bullet in ["- ", "* ", "+ ", "• "] where s.hasPrefix(bullet) {
            return .listItem(marker: "•", depth: depth, text: String(s.dropFirst(bullet.count)))
        }
        let digits = s.prefix(while: \.isNumber)
        if !digits.isEmpty, digits.count <= 3 {
            let rest = s.dropFirst(digits.count)
            if rest.hasPrefix(". ") || rest.hasPrefix(") ") {
                return .listItem(marker: digits + ".", depth: depth, text: String(rest.dropFirst(2)))
            }
        }
        return nil
    }

    /// A cell that reads as a number (`31`, `−4,5 %`, `1 200`, `~240`) — right-aligned in a table.
    public static func isNumeric(_ cell: String) -> Bool {
        let s = cell.trimmingCharacters(in: .whitespaces)
        guard let first = s.first, first.isNumber || "-−+~≈".contains(first) else { return false }
        let allowed = Set("0123456789 \u{00A0}.,%-−+~≈×x")
        return s.contains(where: \.isNumber) && s.allSatisfy { allowed.contains($0) }
    }
}
