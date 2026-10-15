import Foundation
import Testing
@testable import TranscriberCore

/// Кусок настоящего дерева живого звонка в Толке (разведка 23.09.2026, снимок snap-5, подветка
/// `participants` целиком). Правится только новой выгрузкой из настоящей встречи.
private func fixture(_ name: String) -> String {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .appendingPathComponent("Fixtures")
        .appendingPathComponent(name)
    return (try? String(contentsOf: url, encoding: .utf8)) ?? ""
}

private let grid = fixture("talk-grid.txt")

private func tempConfig() -> URL {
    FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString)
        .appendingPathComponent("app-config.json")
}

// MARK: - Разбор сетки

@Test func fixtureLooksLikeTheRealThing() {
    #expect(grid.contains("class='participants'"))
    #expect(grid.split(separator: "\n").count > 100)
}

@Test func parseReadsEveryNamedTile() {
    let snapshot = TalkSnapshot.parse(grid)
    #expect(snapshot?.participants.map(\.name) == [
        "Переговорная 5", "Пётр Кузьмин", "Сергей Орлов", "Елена Волкова", "Дмитрий Лебедев",
    ])
}

@Test func parseReadsMicStates() {
    let snapshot = TalkSnapshot.parse(grid)
    #expect(snapshot?.participants.map(\.micOn) == [true, false, false, false, false])
}

/// Активный — тот, у кого в подветке класс с `active-speaker`; в снимке это переговорка.
@Test func parseFindsTheActiveSpeaker() {
    let snapshot = TalkSnapshot.parse(grid)
    #expect(snapshot?.activeName == "Переговорная 5")
    #expect(snapshot?.participants.filter(\.active).count == 1)
}

/// Плитка пагинации и собственное превью хозяина имени не несут — это не участники.
@Test func parseSkipsTilesWithoutAName() {
    #expect(grid.contains("class='pagination-info'"))
    #expect(TalkSnapshot.parse(grid)?.participants.count == 5)
}

@Test func parseReturnsNilWhenThereIsNoParticipantsGroup() {
    let dump = """
    pid 996, windows 1, roots 1
    --- root 0
    AXWindow title='Транскрибатор — Google Chrome'
      AXGroup class='tab-strip'
        AXButton title='Новая вкладка'
    --- nodes 3
    """
    #expect(TalkSnapshot.parse(dump) == nil)
}

/// Активным считается любой класс, где встречается `active-speaker` — Толк показывал и
/// `zoomed active-speaker dominant`, и голый `active-speaker`.
@Test func parseAcceptsActiveSpeakerWithoutDominant() {
    let dump = """
    AXGroup class='participants'
      AXGroup class='grid-participant'
        AXGroup class='active-speaker'
          AXGroup class='info-container'
            AXButton class='participant-icon global-as-button'
            AXButton title='Иван Сидоров' class='participant-info-content global-as-button'
              AXGroup class='participant-name'
                AXStaticText value='Иван Сидоров'
    """
    #expect(TalkSnapshot.parse(dump)?.participants
            == [TalkParticipant(name: "Иван Сидоров", micOn: true, active: true)])
}

/// Имя берётся из `participant-name`, когда у кнопки нет title — так выглядят плитки ботов.
@Test func parseFallsBackToTheNameLabel() {
    let dump = """
    AXGroup class='participants'
      AXGroup class='grid-participant _animate'
        AXGroup class='wrapper'
          AXGroup class='info-container'
            AXButton class='participant-icon mute global-as-button'
            AXButton class='participant-info-content global-as-button'
              AXGroup class='participant-name'
                AXStaticText value='KTalk Secretary (саммаризатор встреч)'
    """
    #expect(TalkSnapshot.parse(dump)?.participants
            == [TalkParticipant(name: "KTalk Secretary (саммаризатор встреч)", micOn: false, active: false)])
}

/// Группа участников пуста — это всё ещё звонок, а не «дерева нет»: пустой снимок, не nil.
@Test func parseReturnsAnEmptySnapshotForAnEmptyGrid() {
    let dump = """
    AXGroup class='content'
      AXGroup class='participants'
      AXGroup class='bottom'
        AXButton desc='Участники' class='button'
    """
    #expect(TalkSnapshot.parse(dump)?.participants == [])
}

/// Соседи плитки за пределами `participants` в снимок не попадают.
@Test func parseStopsAtTheEndOfTheParticipantsSubtree() {
    let dump = """
    AXGroup class='content'
      AXGroup class='participants'
        AXGroup class='grid-participant'
          AXButton title='Пётр Кузьмин' class='participant-info-content global-as-button'
      AXGroup class='sidebar'
        AXGroup class='grid-participant'
          AXButton title='Не участник' class='participant-info-content global-as-button'
    """
    #expect(TalkSnapshot.parse(dump)?.participants.map(\.name) == ["Пётр Кузьмин"])
}

// MARK: - Строка JSON

@Test func jsonLineHasTheDocumentedShape() {
    let snapshot = TalkSnapshot(participants: [
        TalkParticipant(name: "Пётр Кузьмин", micOn: false, active: false),
        TalkParticipant(name: "Переговорная 5", micOn: true, active: true),
    ])
    #expect(snapshot.jsonLine(t: 12.34) == #"{"t": 12.3, "participants": ["#
            + #"{"name": "Пётр Кузьмин", "mic": false, "active": false}, "#
            + #"{"name": "Переговорная 5", "mic": true, "active": true}]}"#)
}

@Test func jsonLineIsValidJSONWithNamesIntact() throws {
    let snapshot = TalkSnapshot.parse(grid)!
    let data = snapshot.jsonLine(t: 0.05).data(using: .utf8)!
    let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
    #expect(object?["t"] as? Double == 0.1)
    let people = object?["participants"] as? [[String: Any]]
    #expect(people?.count == 5)
    #expect(people?.first?["name"] as? String == "Переговорная 5")
    #expect(people?.first?["active"] as? Bool == true)
}

/// Кавычка в имени не должна ломать строку файла.
@Test func jsonLineEscapesQuotesAndBackslashes() throws {
    let snapshot = TalkSnapshot(participants: [
        TalkParticipant(name: #"Пётр "Стар\" Иванов"#, micOn: true, active: false),
    ])
    let data = snapshot.jsonLine(t: 1).data(using: .utf8)!
    let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
    let people = object?["participants"] as? [[String: Any]]
    #expect(people?.first?["name"] as? String == #"Пётр "Стар\" Иванов"#)
}

// MARK: - Конфиг

@Test func talkConfigDefaultsToOn() {
    #expect(TalkConfig.defaults.enabled)
    #expect(TalkConfig.load(from: tempConfig()) == TalkConfig.defaults)
}

@Test func talkConfigReadsTheBlock() throws {
    let url = tempConfig()
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try #"{"privacy": {}, "talk": {"enabled": false}}"#.write(to: url, atomically: true, encoding: .utf8)
    #expect(TalkConfig.load(from: url).enabled == false)
}

/// Блок дописывается текстом: остальные байты файла остаются на месте.
@Test func talkConfigAddsItsBlockKeepingTheRest() throws {
    let url = tempConfig()
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    let original = """
    {
      "privacy": {
        "_comment": "руками",
        "zoomIsPrivate": true
      }
    }
    """
    try original.write(to: url, atomically: true, encoding: .utf8)
    #expect(TalkConfig.load(from: url) == TalkConfig.defaults)
    let after = try String(contentsOf: url, encoding: .utf8)
    #expect(after.contains(#""_comment": "руками""#))
    #expect(after.contains(#""zoomIsPrivate": true"#))
    let object = try JSONSerialization.jsonObject(with: Data(after.utf8)) as? [String: Any]
    #expect((object?["talk"] as? [String: Any])?["enabled"] as? Bool == true)
    #expect(object?["privacy"] != nil)
}

@Test func talkConfigSurvivesAMalformedBlock() throws {
    let url = tempConfig()
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try #"{"talk": {"enabled": "да"}}"#.write(to: url, atomically: true, encoding: .utf8)
    #expect(TalkConfig.load(from: url) == TalkConfig.defaults)
}

// MARK: - Наблюдатель

/// Наблюдатель на несуществующем pid: дерева нет, файл не появляется, стоп не виснет.
@Test func observerStopsCleanlyWithoutATree() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let store = SessionStore(root: root)
    let dir = try store.create(app: "manual", title: "talk-test", participants: [])
    let observer = TalkObserver(pid: 999_999, sessionDir: dir)
    #expect(observer.url.lastPathComponent == "talk.jsonl")
    observer.start()
    observer.stop()
    observer.stop()   // повторный вызов безопасен
    #expect(observer.lastSnapshot == nil)
    try? FileManager.default.removeItem(at: root)
}


@Test func dominantBeatsRecentActiveSpeaker() {
    let dump = """
    AXGroup class='participants' frame=0,0 10x10
      AXGroup class='grid-participant _animate' frame=0,0 10x10
        AXGroup class='active-speaker' frame=0,0 10x10
        AXGroup class='info-container' frame=0,0 10x10
          AXButton class='participant-icon mute global-as-button' frame=0,0 10x10
          AXButton title='Николай Фомин' class='participant-info-content global-as-button' frame=0,0 10x10
      AXGroup class='grid-participant _animate' frame=0,0 10x10
        AXGroup class='zoomed active-speaker dominant' frame=0,0 10x10
        AXGroup class='info-container' frame=0,0 10x10
          AXButton class='participant-icon mute global-as-button' frame=0,0 10x10
          AXButton title='Татьяна Белова' class='participant-info-content global-as-button' frame=0,0 10x10
    """
    let snap = TalkSnapshot.parse(dump)!
    #expect(snap.participants.map { $0.active } == [false, true])
    let noDominant = dump.replacingOccurrences(of: "zoomed active-speaker dominant", with: "active-speaker")
    #expect(TalkSnapshot.parse(noDominant)!.participants.map { $0.active } == [true, true])
}
