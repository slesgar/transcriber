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
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var listIndents: [Int] = []   // indents of the open list levels, to count depth whatever the step
        var i = 0

        /// Lines of one paragraph keep their breaks (`**Кто:** …` / `**Когда:** …` are two lines):
        /// U+2028 breaks the line without starting a new paragraph.
        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: lineBreak))) }
            paragraph = []
        }

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty { flush(); i += 1; continue }
            if listItem(line) == nil { listIndents = [] }

            if isFenceOpen(trimmed) {   // fenced code: everything up to a line of backticks only
                flush()
                var code: [String] = []
                i += 1
                while i < lines.count, !isFenceClose(lines[i].trimmingCharacters(in: .whitespaces)) {
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
                var header = cells(trimmed)
                var rows: [[String]] = []
                i += 2
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("|") {
                    rows.append(cells(lines[i].trimmingCharacters(in: .whitespaces)))
                    i += 1
                }
                // a row wider than the header keeps its cells: the table grows, nothing is dropped
                let columns = max(header.count, rows.map(\.count).max() ?? 0)
                header += Array(repeating: "", count: columns - header.count)
                rows = rows.map { $0 + Array(repeating: "", count: columns - $0.count) }
                let a = Array((aligns + Array(repeating: .left, count: max(0, columns - aligns.count))).prefix(columns))
                blocks.append(.table(header: header, alignments: a, rows: rows))
                continue
            }

            if case .listItem(let marker, let indent, let body)? = listItem(line),
               paragraph.isEmpty || marker == "•" || marker == "1." {   // «12. декабря» mid-paragraph is text
                flush()
                while let last = listIndents.last, last > indent { listIndents.removeLast() }
                if listIndents.last.map({ $0 < indent }) ?? true { listIndents.append(indent) }
                blocks.append(.listItem(marker: marker, depth: min(listIndents.count - 1, 3), text: body))
                i += 1
                continue
            }

            if trimmed.hasPrefix(">") {
                flush()
                var quote: [String] = []
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    quote.append(String(lines[i].trimmingCharacters(in: .whitespaces).dropFirst()).trimmingCharacters(in: .whitespaces))
                    i += 1
                }
                blocks.append(.quote(quote.filter { !$0.isEmpty }.joined(separator: lineBreak)))
                continue
            }

            paragraph.append(trimmed)
            i += 1
        }
        flush()
        return blocks
    }

    public static let lineBreak = "\u{2028}"

    static func isFenceOpen(_ s: String) -> Bool {
        guard s.hasPrefix("```") else { return false }
        return s.dropFirst(3).allSatisfy { $0.isLetter || $0.isNumber || "+-_".contains($0) }
    }

    static func isFenceClose(_ s: String) -> Bool { s.count >= 3 && s.allSatisfy { $0 == "`" } }

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
        let depth = indent   // raw indent; parse() turns it into a nesting level
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
        // a date or a phone is not a quantity
        if s.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil { return false }
        return s.contains(where: \.isNumber) && s.allSatisfy { allowed.contains($0) }
    }
}
