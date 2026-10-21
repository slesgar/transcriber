import AppKit
import TranscriberCore

/// An agent's answer as rich text for the answers sheet: `MarkdownBlocks` gives the structure,
/// Apple's inline parser the emphasis inside each block. Tables are real `NSTextTable`s — a
/// header row on a light fill, hairline borders, numbers right-aligned — so a «сколько и где»
/// answer reads at a glance during a meeting.
enum AnswerRenderer {
    static let font = NSFont.systemFont(ofSize: 13)
    static let small = NSFont.systemFont(ofSize: 12)

    static func render(_ text: String) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let blocks = MarkdownBlocks.parse(text)
        for (i, block) in blocks.enumerated() {
            let last = i == blocks.count - 1
            // text right under a table needs air: the table's bottom border sits on the baseline
            var afterTable: CGFloat = 0
            if i > 0, case .table = blocks[i - 1] { afterTable = 8 }
            switch block {
            case .paragraph(let s):
                out.append(inline(s, paragraph: para(before: afterTable, after: 6)))
            case .heading(let level, let s):
                let size: CGFloat = level <= 2 ? 14 : 13
                out.append(inline(s, base: .systemFont(ofSize: size, weight: .semibold), paragraph: para(before: 4, after: 4)))
            case .listItem(let marker, let depth, let s):
                let indent = CGFloat(depth) * 16
                let p = para(after: 3)
                p.firstLineHeadIndent = indent
                p.headIndent = indent + 18
                p.tabStops = [NSTextTab(textAlignment: .left, location: indent + 18)]
                let line = NSMutableAttributedString(string: marker + "\t", attributes: [
                    .font: font, .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: p])
                line.append(inline(s, paragraph: p))
                out.append(line)
            case .table(let header, let alignments, let rows):
                out.append(table(header: header, alignments: alignments, rows: rows))
            case .code(let s):
                let block = NSTextBlock()
                block.backgroundColor = NSColor.quaternaryLabelColor.withAlphaComponent(0.12)
                block.setWidth(8, type: .absoluteValueType, for: .padding)
                block.setValue(100, type: .percentageValueType, for: .width)   // without it the block collapses
                let p = para(after: 6)
                p.textBlocks = [block]
                out.append(NSAttributedString(string: s, attributes: [
                    .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
                    .foregroundColor: NSColor.labelColor, .paragraphStyle: p]))
            case .quote(let s):
                let bar = NSTextBlock()
                bar.setWidth(2, type: .absoluteValueType, for: .border, edge: .minX)
                bar.setBorderColor(.tertiaryLabelColor, for: .minX)
                bar.setWidth(10, type: .absoluteValueType, for: .padding, edge: .minX)
                bar.setWidth(2, type: .absoluteValueType, for: .padding, edge: .minY)
                bar.setWidth(2, type: .absoluteValueType, for: .padding, edge: .maxY)
                bar.setValue(100, type: .percentageValueType, for: .width)
                let p = para(after: 6)
                p.textBlocks = [bar]
                out.append(inline(s, color: .secondaryLabelColor, paragraph: p))
            case .rule:
                let p = para(before: 2, after: 6)
                out.append(NSAttributedString(string: "\u{00A0}", attributes: [
                    .font: NSFont.systemFont(ofSize: 4), .paragraphStyle: p,
                    .strikethroughStyle: NSUnderlineStyle.single.rawValue, .strikethroughColor: NSColor.separatorColor]))
            }
            if !last { out.append(NSAttributedString(string: "\n", attributes: [.font: font])) }
        }
        return out
    }

    private static func para(before: CGFloat = 0, after: CGFloat) -> NSMutableParagraphStyle {
        let p = NSMutableParagraphStyle()
        p.lineSpacing = 2
        p.paragraphSpacingBefore = before
        p.paragraphSpacing = after
        return p
    }

    /// One block's text with its inline marks; links are dropped — the transcript is not to be clicked.
    static func inline(_ text: String, base: NSFont = font, color: NSColor = .labelColor,
                       paragraph: NSParagraphStyle) -> NSAttributedString {
        let attrs: [NSAttributedString.Key: Any] = [.font: base, .foregroundColor: color, .paragraphStyle: paragraph]
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard let parsed = try? NSMutableAttributedString(markdown: text, options: options) else {
            return NSAttributedString(string: text, attributes: attrs)
        }
        let full = NSRange(location: 0, length: parsed.length)
        parsed.removeAttribute(.link, range: full)
        parsed.addAttributes(attrs, range: full)
        parsed.enumerateAttribute(.inlinePresentationIntent, in: full) { value, range, _ in
            guard let raw = (value as? NSNumber)?.uintValue else { return }
            let intent = InlinePresentationIntent(rawValue: raw)
            var f = base
            if intent.contains(.stronglyEmphasized) { f = .systemFont(ofSize: base.pointSize, weight: .semibold) }
            if intent.contains(.code) { f = .monospacedSystemFont(ofSize: base.pointSize - 1, weight: .regular) }
            if intent.contains(.emphasized) { f = NSFontManager.shared.convert(f, toHaveTrait: .italicFontMask) }
            parsed.addAttribute(.font, value: f, range: range)
        }
        return parsed
    }

    /// Columns that hold only numbers are right-aligned even when the agent did not mark them.
    private static func table(header: [String], alignments: [MarkdownBlock.Alignment], rows: [[String]]) -> NSAttributedString {
        let table = NSTextTable()
        table.numberOfColumns = header.count
        table.collapsesBorders = true
        table.hidesEmptyCells = false
        table.setContentWidth(100, type: .percentageValueType)
        let numeric = (0..<header.count).map { c in
            !rows.isEmpty && rows.allSatisfy { $0[c].isEmpty || MarkdownBlocks.isNumeric($0[c]) }
        }
        // numbers need little room; the text columns share what is left, so labels do not wrap
        // a number column is as wide as its header asks (about 2% per letter of a 460 pt sheet)
        let shares: [CGFloat] = header.map { min(max(CGFloat($0.count) * 1.75 + 6, 12), 28) }
        let textColumns = numeric.filter { !$0 }.count
        let numericTotal = zip(shares, numeric).filter { $0.1 }.map(\.0).reduce(0, +)
        let textShare = textColumns == 0 ? 0 : max(100 - numericTotal, 30) / CGFloat(textColumns)
        let out = NSMutableAttributedString()
        for (r, row) in ([header] + rows).enumerated() {
            for (c, cell) in row.enumerated() {
                let block = NSTextTableBlock(table: table, startingRow: r, rowSpan: 1, startingColumn: c, columnSpan: 1)
                block.setWidth(0.5, type: .absoluteValueType, for: .border)
                block.setBorderColor(.separatorColor)
                block.setWidth(4, type: .absoluteValueType, for: .padding, edge: .minY)
                block.setWidth(4, type: .absoluteValueType, for: .padding, edge: .maxY)
                block.setWidth(8, type: .absoluteValueType, for: .padding, edge: .minX)
                block.setWidth(8, type: .absoluteValueType, for: .padding, edge: .maxX)
                if textColumns > 0 {
                    block.setValue(numeric[c] ? shares[c] : textShare, type: .percentageValueType, for: .width)
                }
                if r == 0 { block.backgroundColor = NSColor.quaternaryLabelColor.withAlphaComponent(0.15) }
                let p = NSMutableParagraphStyle()
                p.textBlocks = [block]
                switch c < alignments.count ? alignments[c] : .left {
                case .center: p.alignment = .center
                case .right: p.alignment = .right
                case .left: p.alignment = numeric[c] && r > 0 ? .right : .left
                }
                if r == 0 && numeric[c] && alignments[c] == .left { p.alignment = .right }
                let base: NSFont = r == 0 ? .systemFont(ofSize: 12, weight: .semibold)
                    : (numeric[c] ? .monospacedDigitSystemFont(ofSize: 12, weight: .regular) : small)
                let cellText = NSMutableAttributedString(attributedString: inline(cell, base: base, paragraph: p))
                cellText.append(NSAttributedString(string: "\n", attributes: [.font: base, .paragraphStyle: p]))
                out.append(cellText)
            }
        }
        // the table's last newline ends the block; the caller adds the separator before the next block
        if out.length > 0, out.string.hasSuffix("\n") { out.deleteCharacters(in: NSRange(location: out.length - 1, length: 1)) }
        return out
    }
}
