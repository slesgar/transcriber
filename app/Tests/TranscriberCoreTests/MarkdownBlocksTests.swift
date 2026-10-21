import Testing
@testable import TranscriberCore

@Test func pipeTableBecomesATable() {
    let text = """
    По методологии:

    | Срез | Параметров | Фильтров |
    |---|---:|:--:|
    | В проде | 31 | 40 |
    | После ТЗ | 48 |

    Итог.
    """
    #expect(MarkdownBlocks.parse(text) == [
        .paragraph("По методологии:"),
        .table(header: ["Срез", "Параметров", "Фильтров"], alignments: [.left, .right, .center],
               rows: [["В проде", "31", "40"], ["После ТЗ", "48", ""]]),
        .paragraph("Итог."),
    ])
}

@Test func listsHeadingsQuotesCodeAndRules() {
    let text = """
    ## Кто что делает
    - Иван: сравнение
      - к пятнице
    2. Мария: доступ
    > сказано вскользь
    ---
    ```
    select 1
    ```
    """
    #expect(MarkdownBlocks.parse(text) == [
        .heading(level: 2, text: "Кто что делает"),
        .listItem(marker: "•", depth: 0, text: "Иван: сравнение"),
        .listItem(marker: "•", depth: 1, text: "к пятнице"),
        .listItem(marker: "2.", depth: 0, text: "Мария: доступ"),
        .quote("сказано вскользь"),
        .rule,
        .code("select 1"),
    ])
}

@Test func linesOfOneParagraphKeepTheirBreaksAndPipesInsideTextAreNotTables() {
    #expect(MarkdownBlocks.parse("**Кто:** Иван\n**Когда:** пятница") == [.paragraph("**Кто:** Иван\u{2028}**Когда:** пятница")])
    #expect(MarkdownBlocks.parse("| одна строка без разделителя |") == [.paragraph("| одна строка без разделителя |")])
}

@Test func escapedPipeStaysInTheCell() {
    #expect(MarkdownBlocks.cells(#"| a \| b | c |"#) == ["a | b", "c"])
}

@Test func numericCellsAreRecognised() {
    for s in ["31", "−4,5 %", "1 200", "~240", "66"] { #expect(MarkdownBlocks.isNumeric(s), "\(s)") }
    for s in ["В проде", "Q4", "", "31 параметр", "2026-10-21"] { #expect(!MarkdownBlocks.isNumeric(s), "\(s)") }
}

@Test func oneLineFenceIsTextAndInnerFenceWithLanguageDoesNotClose() {
    #expect(MarkdownBlocks.parse("```select 1```\nдальше") == [.paragraph("```select 1```\u{2028}дальше")])
    #expect(MarkdownBlocks.parse("```\n```bash\nls\n```") == [.code("```bash\nls")])
}

@Test func widerRowGrowsTheTable() {
    #expect(MarkdownBlocks.parse("| a | b |\n|---|---|\n| 1 | 2 | 3 |") ==
            [.table(header: ["a", "b", ""], alignments: [.left, .left, .left], rows: [["1", "2", "3"]])])
}

@Test func nestingCountsLevelsNotSpaces() {
    let blocks = MarkdownBlocks.parse("- a\n    - b\n        - c\n" + String(repeating: " ", count: 40) + "- d")
    #expect(blocks.map { if case .listItem(_, let d, _) = $0 { return d } else { return -1 } } == [0, 1, 2, 3])
}

@Test func numberedLineInsideAParagraphStaysText() {
    #expect(MarkdownBlocks.parse("Встреча перенесена на\n12. декабря") == [.paragraph("Встреча перенесена на\u{2028}12. декабря")])
}
