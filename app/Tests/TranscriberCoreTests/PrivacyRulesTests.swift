import Foundation
import Testing
@testable import TranscriberCore

private let calendar = Calendar.current

/// 2026-09-21 is a Monday and 2026-09-24 the Thursday of the same week — asserted below rather
/// than trusted, so a wrong date fails here instead of silently weakening every other case.
private func at(_ day: Int, _ hour: Int, _ minute: Int) -> Date {
    calendar.date(from: DateComponents(year: 2026, month: 9, day: day, hour: hour, minute: minute))!
}

private func tempConfig() -> URL {
    FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString)
        .appendingPathComponent("app-config.json")
}

/// Умолчания нейтральны, поэтому тесты правил задают своё окно и приложение явно:
/// вторник 12:00–12:50 (2026-09-22) и Zoom.
private let rules = PrivacyRules(windows: [PrivateWindow(weekday: 3, from: "12:00", to: "12:50")],
                                 apps: ["com.example.chat"])

@Test func weekdayNumbersMatchCalendar() {
    #expect(calendar.component(.weekday, from: at(21, 12, 0)) == 2)   // понедельник
    #expect(calendar.component(.weekday, from: at(22, 12, 0)) == 3)   // вторник
}

@Test func defaultsAreNeutral() {
    let d = PrivacyRules.defaults
    #expect(d.windows.isEmpty)
    #expect(d.apps.isEmpty)
    #expect(d.workHosts.isEmpty)
    #expect(d.workApps == ["kontur.talk"])
    #expect(d.isPrivate(app: "com.example.chat", at: Date()) == false)
}

@Test func loadWritesDefaultsIntoMissingDirectory() {
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString)
        .appendingPathComponent("nested")
        .appendingPathComponent("app-config.json")
    #expect(PrivacyRules.load(from: url) == .defaults)
    #expect(FileManager.default.fileExists(atPath: url.path))
}

@Test func privateWindowIsPrivate() {
    #expect(rules.isPrivate(app: "com.google.Chrome", at: at(22, 12, 1)))
    #expect(rules.isPrivate(app: "com.apple.Safari", at: at(22, 12, 40)))
}

@Test func workHourInTalkIsNotPrivate() {
    #expect(!rules.isPrivate(app: "kontur.talk", at: at(21, 14, 0)))
    #expect(!rules.isPrivate(app: "kontur.talk", at: at(24, 11, 30)))
    #expect(!rules.isPrivate(app: "com.google.Chrome", at: at(21, 12, 1)))   // понедельник, то же время
}

/// Приватных звонков в Толке не бывает, а рабочие встречи бывают только там — поэтому Толк перебивает окно.
@Test func talkIsNeverPrivateEvenInsideAWindow() {
    #expect(!rules.isPrivate(app: "kontur.talk", at: at(22, 12, 10)))
    #expect(!rules.isPrivate(app: "kontur.talk", at: at(22, 12, 45)))
    #expect(!rules.isPrivate(app: "kontur.talk.helper", at: at(22, 12, 10)))   // префикс, не точное совпадение
}

/// В браузере bundle id не говорит ничего: рабочий звонок узнаётся только по открытой вкладке,
/// и ответ на этот вопрос приходит снаружи, флагом `workContext`.
@Test func workContextInBrowserBeatsTheWindow() {
    #expect(!rules.isPrivate(app: "com.google.Chrome", at: at(22, 12, 10), workContext: true))
    #expect(rules.isPrivate(app: "com.google.Chrome", at: at(22, 12, 10), workContext: false))
    #expect(!rules.isPrivate(app: "com.google.Chrome", at: at(22, 14, 0), workContext: false))
}

@Test func zoomIsPrivateAtAnyTime() {
    #expect(rules.isPrivate(app: "com.example.chat", at: at(22, 11, 0)))
    #expect(rules.isPrivate(app: "com.example.chat.helper", at: at(23, 9, 15)))   // префикс, не точное совпадение
}

@Test func windowBoundariesAreInclusive() {
    #expect(!rules.isPrivate(app: "com.google.Chrome", at: at(22, 11, 59)))
    #expect(rules.isPrivate(app: "com.google.Chrome", at: at(22, 12, 0)))
    #expect(rules.isPrivate(app: "com.google.Chrome", at: at(22, 12, 50)))
    #expect(!rules.isPrivate(app: "com.google.Chrome", at: at(22, 12, 51)))
}

@Test func customRulesFromJSON() throws {
    let url = tempConfig()
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try """
    {"privacy": {"windows": [{"weekday": 3, "from": "09:00", "to": "10:00"}], "apps": ["com.example.diary"]}}
    """.write(to: url, atomically: true, encoding: .utf8)
    let rules = PrivacyRules.load(from: url)
    #expect(rules.windows == [PrivateWindow(weekday: 3, from: "09:00", to: "10:00")])
    #expect(calendar.component(.weekday, from: at(22, 9, 30)) == 3)   // вторник
    #expect(rules.isPrivate(app: "com.google.Chrome", at: at(22, 9, 30)))
    #expect(rules.isPrivate(app: "com.example.diary", at: at(21, 12, 0)))
    #expect(!rules.isPrivate(app: "com.google.Chrome", at: at(22, 12, 1)))   // окна из другого набора правил не действуют
    // Ключей workApps/workHosts в файле нет — берутся дефолты, Толк остаётся рабочим.
    #expect(rules.workApps == PrivacyRules.defaultWorkApps)
    #expect(rules.workHosts == PrivacyRules.defaultWorkHosts)
    #expect(!rules.isPrivate(app: "kontur.talk", at: at(22, 9, 30)))
}

@Test func workKeysComeFromJSONWhenPresent() throws {
    let url = tempConfig()
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try """
    {"privacy": {"windows": [{"weekday": 3, "from": "12:00", "to": "12:50"}], "apps": [], \
    "workApps": ["com.example.work"], "workHosts": ["calls.example.com"]}}
    """.write(to: url, atomically: true, encoding: .utf8)
    let rules = PrivacyRules.load(from: url)
    #expect(rules.workApps == ["com.example.work"])
    #expect(rules.workHosts == ["calls.example.com"])
    #expect(!rules.isPrivate(app: "com.example.work", at: at(22, 12, 10)))
    #expect(rules.isPrivate(app: "kontur.talk", at: at(22, 12, 10)))   // список переопределён целиком
}

@Test func missingFileIsWrittenWithDefaults() throws {
    let url = tempConfig()
    #expect(PrivacyRules.load(from: url) == .defaults)
    #expect(FileManager.default.fileExists(atPath: url.path))
    let written = PrivacyRules.load(from: url)   // второй заход читает уже файл, не ветку «нет файла»
    #expect(written == .defaults)
    let json = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
    let privacy = json["privacy"] as? [String: Any]
    #expect(privacy != nil)
    #expect(privacy?["workApps"] != nil)    // новые ключи видны в файле, есть что править руками
    #expect(privacy?["workHosts"] != nil)
    #expect(json["_comment"] != nil)   // подсказка про нумерацию дней недели для ручной правки
}

@Test func brokenFileFallsBackToDefaults() throws {
    let url = tempConfig()
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try "{ не json".write(to: url, atomically: true, encoding: .utf8)
    #expect(PrivacyRules.load(from: url) == .defaults)

    let other = tempConfig()
    try FileManager.default.createDirectory(at: other.deletingLastPathComponent(), withIntermediateDirectories: true)
    try #"{"privacy": {"windows": "каждый понедельник"}}"#.write(to: other, atomically: true, encoding: .utf8)
    #expect(PrivacyRules.load(from: other) == .defaults)

    let unrelated = tempConfig()
    try FileManager.default.createDirectory(at: unrelated.deletingLastPathComponent(), withIntermediateDirectories: true)
    try #"{"worker": {"model": "large"}}"#.write(to: unrelated, atomically: true, encoding: .utf8)
    #expect(PrivacyRules.load(from: unrelated) == .defaults)
}

@Test func malformedTimeDisablesOnlyThatWindow() {
    let rules = PrivacyRules(windows: [PrivateWindow(weekday: 2, from: "четыре", to: "17:30"),
                                       PrivateWindow(weekday: 2, from: "19:00", to: "20:00")],
                             apps: [])
    #expect(!rules.isPrivate(app: "com.google.Chrome", at: at(21, 16, 1)))
    #expect(rules.isPrivate(app: "com.google.Chrome", at: at(21, 19, 30)))
}
