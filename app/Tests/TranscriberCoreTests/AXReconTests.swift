import Foundation
import Testing
@testable import TranscriberCore

// MARK: - Normalization

@Test func normalizeStripsIndentationAndFrame() {
    let line = "      AXGroup class='participant speaking' selected=true frame=-838,-1415 2560x1415"
    #expect(AXReconText.normalize(line) == "AXGroup class='participant speaking' selected=true")
}

@Test func normalizeKeepsALineThatHasNoFrame() {
    #expect(AXReconText.normalize("  AXStaticText value='Иван Сидоров'")
            == "AXStaticText value='Иван Сидоров'")
}

@Test func normalizeLeavesFrameLikeTextInsideAValueAlone() {
    let line = "  AXStaticText value='frame=1,2 3x4 в тексте' frame=0,0 10x10"
    #expect(AXReconText.normalize(line) == "AXStaticText value='frame=1,2 3x4 в тексте'")
}

@Test func normalizedLinesDropHeaderAndSeparators() {
    let dump = """
    pid 996, windows 1, roots 1
    --- root 0
    AXWindow title='Толк' frame=0,0 100x100
      AXGroup class='tile' frame=1,2 30x40
    --- nodes 2
    """
    #expect(AXReconText.normalizedLines(from: dump)
            == ["AXWindow title='Толк'", "AXGroup class='tile'"])
}

// MARK: - Diff

@Test func diffReportsAppearedAndDisappearedLines() {
    let before = ["AXGroup class='tile'", "AXStaticText value='Иван Сидоров'"]
    let after = ["AXGroup class='tile speaking'", "AXStaticText value='Иван Сидоров'"]
    #expect(AXReconText.diff(from: before, to: after) == [
        AXChange(kind: .added, line: "AXGroup class='tile speaking'"),
        AXChange(kind: .removed, line: "AXGroup class='tile'"),
    ])
}

@Test func diffCountsRepeatedIdenticalLines() {
    let before = ["AXGroup class='tile'", "AXGroup class='tile'", "AXGroup class='tile'"]
    let after = ["AXGroup class='tile'"]
    #expect(AXReconText.diff(from: before, to: after)
            == [AXChange(kind: .removed, line: "AXGroup class='tile'", count: 2)])
}

@Test func diffOfIdenticalSnapshotsIsEmpty() {
    let lines = ["AXWindow title='Толк'", "AXGroup class='tile'", "AXGroup class='tile'"]
    #expect(AXReconText.diff(from: lines, to: lines).isEmpty)
}

@Test func diffAgainstAnEmptyPreviousSnapshotIsAllAdditions() {
    #expect(AXReconText.diff(from: [], to: ["AXWindow title='Толк'"])
            == [AXChange(kind: .added, line: "AXWindow title='Толк'")])
}

// MARK: - Counting

@Test func roleCountsTallyTheFirstTokenOfEachLine() {
    let lines = ["AXGroup class='a'", "AXGroup class='b'", "AXStaticText value='Иван Сидоров'"]
    #expect(AXReconText.roleCounts(from: lines) == ["AXGroup": 2, "AXStaticText": 1])
}

@Test func staticTextValuesAreTakenOnlyFromStaticTextNodesAndDeduplicated() {
    let lines = [
        "AXStaticText value='Иван Сидоров'",
        "AXStaticText value='Иван Сидоров'",
        "AXButton desc='Выключить микрофон' value='Ольга Смирнова'",
        "AXStaticText subrole='AXContentList' value='Ольга Смирнова'",
        "AXStaticText title='без значения'",
    ]
    #expect(AXReconText.staticTextValues(from: lines) == ["Иван Сидоров", "Ольга Смирнова"])
}

// MARK: - Timestamps

@Test func stampIsMinutesSecondsAndTenths() {
    #expect(AXReconText.stamp(0) == "00:00.0")
    #expect(AXReconText.stamp(63.24) == "01:03.2")
    #expect(AXReconText.stamp(-5) == "00:00.0")
    // 59.97 s rounds to a whole minute rather than to «00:60.0»
    #expect(AXReconText.stamp(59.97) == "01:00.0")
}

@Test func changeLineCarriesSignAndRepeatCount() {
    #expect(AXReconText.changeLine(AXChange(kind: .added, line: "AXGroup class='x'"), at: 12.34)
            == "00:12.3 + AXGroup class='x'")
    #expect(AXReconText.changeLine(AXChange(kind: .removed, line: "AXGroup class='x'", count: 3), at: 5)
            == "00:05.0 - 3× AXGroup class='x'")
}

// MARK: - Summary

@Test func summaryCountsRolesRanksChangesAndFishesOutNames() throws {
    var stats = AXReconText.Stats()
    stats.samples = 12
    stats.snapshots = 4
    stats.changeEvents = 7
    stats.duration = 180
    stats.roleCounts = ["AXGroup": 40, "AXStaticText": 12, "AXButton": 12]
    stats.changeTally = ["AXGroup class='tile speaking'": 9, "AXGroup class='tile'": 9,
                         "AXStaticText value='00:12'": 2]
    stats.staticTexts = ["Иван Сидоров", "Выключить микрофон у всех", "Ольга Смирнова", "00:12"]
    let text = AXReconText.summary(sessionID: "2026-09-23-1400", stats: stats)
    #expect(text.contains("recon сессии 2026-09-23-1400"))
    #expect(text.contains("сэмплов 12, снимков 4, событий изменения 7, длительность 03:00.0"))
    #expect(text.contains("40\tAXGroup"))
    // Equal counts fall back to the line itself ascending, so the order is stable between runs
    // («…tile speaking'» sorts before «…tile'»: a space beats an apostrophe).
    let speaking = try #require(text.range(of: "9\tAXGroup class='tile speaking'"))
    let idle = try #require(text.range(of: "9\tAXGroup class='tile'"))
    #expect(speaking.lowerBound < idle.lowerBound)
    #expect(text.contains("Иван Сидоров"))
    #expect(text.contains("Ольга Смирнова"))
    // Not a name: a button label and a timer must not reach the participant list.
    #expect(!text.contains("== кандидаты в участники (4"))
    #expect(text.contains("== кандидаты в участники (2 из 4 AXStaticText) =="))
}

@Test func summaryKeepsOnlyTheThirtyMostChangingLines() {
    var stats = AXReconText.Stats()
    for i in 1...50 { stats.changeTally["AXGroup id='n\(i)'"] = i }
    let text = AXReconText.summary(sessionID: "s", stats: stats)
    #expect(text.contains("50\tAXGroup id='n50'"))
    #expect(text.contains("21\tAXGroup id='n21'"))
    #expect(!text.contains("20\tAXGroup id='n20'"))
}

@Test func summaryOfAnEmptyRunSaysSoInsteadOfCrashing() {
    let text = AXReconText.summary(sessionID: "s", stats: AXReconText.Stats())
    #expect(text.contains("сэмплов 0, снимков 0"))
    #expect(text.contains("—"))
}

// MARK: - Config

@Test func reconConfigDefaultsWhenTheBlockIsAbsent() throws {
    let url = try tempConfig(#"{"privacy": {"windows": [], "apps": []}}"#)
    defer { try? FileManager.default.removeItem(at: url) }
    #expect(ReconConfig.load(from: url) == ReconConfig.defaults)
}

@Test func reconConfigIsReadFromTheReconBlock() throws {
    let url = try tempConfig(#"{"privacy": {}, "recon": {"enabled": false, "seconds": 42}}"#)
    defer { try? FileManager.default.removeItem(at: url) }
    #expect(ReconConfig.load(from: url) == ReconConfig(enabled: false, seconds: 42))
}

@Test func reconConfigTakesTheHalfOfABlockThatIsThere() throws {
    let url = try tempConfig(#"{"recon": {"seconds": 30}}"#)
    defer { try? FileManager.default.removeItem(at: url) }
    #expect(ReconConfig.load(from: url) == ReconConfig(enabled: true, seconds: 30))
}

@Test func reconConfigClampsAbsurdDurations() throws {
    let url = try tempConfig(#"{"recon": {"seconds": 999999}}"#)
    defer { try? FileManager.default.removeItem(at: url) }
    #expect(ReconConfig.load(from: url).seconds == ReconConfig.maxSeconds)
}

@Test func reconConfigLeavesAMissingFileAlone() {
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("recon-absent-\(UUID().uuidString).json")
    #expect(ReconConfig.load(from: url) == ReconConfig.defaults)
    #expect(!FileManager.default.fileExists(atPath: url.path))
}

@Test func loadingAConfigWithoutReconWritesTheBlockAndLeavesPrivacyByteForByte() throws {
    let original = """
    {
      "_comment": "не трогать",
      "privacy": {
        "apps": [
          "com.example.chat"
        ],
        "windows": [
          {
            "weekday": 3,
            "from": "12:00",
            "to": "12:50"
          }
        ]
      }
    }
    """
    let url = try tempConfig(original)
    defer { try? FileManager.default.removeItem(at: url) }
    #expect(ReconConfig.load(from: url) == ReconConfig.defaults)
    let after = try String(contentsOf: url, encoding: .utf8)
    // Everything the file had before its closing brace — the comment and the hand-kept key order
    // inside `privacy` included — survives byte for byte.
    #expect(after.hasPrefix(String(original.dropLast(2))))
    #expect(after.contains("\"recon\": {"))
    #expect(after.contains("\"enabled\": true"))
    #expect(after.contains("\"seconds\": 180"))
    // And the result is still valid JSON that both readers can parse.
    let data = try Data(contentsOf: url)
    let object = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    #expect(object["_comment"] as? String == "не трогать")
    #expect(ReconConfig.load(from: url) == ReconConfig.defaults)
    #expect(PrivacyRules.load(from: url).apps == ["com.example.chat"])
    #expect(PrivacyRules.load(from: url).windows == [PrivateWindow(weekday: 3, from: "12:00", to: "12:50")])
}

@Test func insertingLastMemberAppendsAfterTheLastKeyAndRefusesNonObjects() {
    #expect(ReconConfig.insertingLastMember("  \"b\": 2", into: "{\n  \"a\": 1\n}")
            == "{\n  \"a\": 1,\n  \"b\": 2\n}\n")
    // An object with nothing in it takes no comma.
    #expect(ReconConfig.insertingLastMember("  \"b\": 2", into: "{}") == "{\n  \"b\": 2\n}\n")
    #expect(ReconConfig.insertingLastMember("  \"b\": 2", into: "[1, 2]") == nil)
}

private func tempConfig(_ text: String) throws -> URL {
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("recon-config-\(UUID().uuidString).json")
    try text.write(to: url, atomically: true, encoding: .utf8)
    return url
}
