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

@Test func linesOfOneParagraphJoinAndPipesInsideTextAreNotTables() {
    #expect(MarkdownBlocks.parse("первая строка\nвторая | с чертой") == [.paragraph("первая строка вторая | с чертой")])
    #expect(MarkdownBlocks.parse("| одна строка без разделителя |") == [.paragraph("| одна строка без разделителя |")])
}

@Test func escapedPipeStaysInTheCell() {
    #expect(MarkdownBlocks.cells(#"| a \| b | c |"#) == ["a | b", "c"])
}

@Test func numericCellsAreRecognised() {
    for s in ["31", "−4,5 %", "1 200", "~240", "66"] { #expect(MarkdownBlocks.isNumeric(s), "\(s)") }
    for s in ["В проде", "Q4", "", "31 параметр"] { #expect(!MarkdownBlocks.isNumeric(s), "\(s)") }
}
