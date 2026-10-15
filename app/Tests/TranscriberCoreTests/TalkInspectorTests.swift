import Foundation
import Testing
@testable import TranscriberCore

@Test func participantHeuristicKeepsPersonNamesOnly() {
    let texts = ["Иван Сидоров", "Вы", "Мария Иванова", "Включить микрофон", "Иван Сидоров", "Анна-Мария Соколова",
                 "Участники (3)", "Анна Петрова", "John Smith", "чат"]
    #expect(TalkInspector.participantNames(from: texts) == ["Иван Сидоров", "Мария Иванова", "Анна-Мария Соколова", "Анна Петрова", "John Smith"])
}

@Test func titleStripsOnlyTheTrailingAppSuffix() {
    #expect(TalkInspector.cleanTitle("Синк — Толк") == "Синк")
    #expect(TalkInspector.cleanTitle("Толк про роадмап") == "Толк про роадмап")
    #expect(TalkInspector.cleanTitle("Толк") == "")
}
