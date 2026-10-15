import Foundation
import Testing
@testable import TranscriberCore

private func tempRoot() throws -> URL {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir
}

private let noon = Calendar.current.date(from: DateComponents(year: 2026, month: 9, day: 15, hour: 14, minute: 30, second: 5))!

@Test func createWritesMetaWithLocalOffset() throws {
    let store = SessionStore(root: try tempRoot())
    let s = try store.create(app: "kontur.talk", title: "Синк", participants: ["Иван Сидоров"], now: noon)
    #expect(s.id == "2026-09-15-1430")
    #expect(FileManager.default.fileExists(atPath: s.metaURL.path))
    let json = try JSONSerialization.jsonObject(with: Data(contentsOf: s.metaURL)) as! [String: Any]
    #expect(json["id"] as? String == "2026-09-15-1430")
    #expect(json["app"] as? String == "kontur.talk")
    #expect(json["title"] as? String == "Синк")
    #expect(json["participants"] as? [String] == ["Иван Сидоров"])
    let started = json["started_at"] as! String
    #expect(started.hasPrefix("2026-09-15T14:30:05"))
    #expect(started.count == "2026-09-15T14:30:05+03:00".count)   // has an explicit offset, not Z
    #expect(!started.hasSuffix("Z"))
    #expect(s.micURL.lastPathComponent == "mic.pcm" && s.sysURL.lastPathComponent == "sys.pcm")
}

@Test func privateFlagOnlyWhenSet() throws {
    let store = SessionStore(root: try tempRoot())
    let open = try store.create(app: "kontur.talk", title: "Синк", participants: [], now: noon)
    let hidden = try store.create(app: "us.zoom", title: "", participants: [], now: noon, private: true)
    let openMeta = try JSONSerialization.jsonObject(with: Data(contentsOf: open.metaURL)) as! [String: Any]
    let hiddenMeta = try JSONSerialization.jsonObject(with: Data(contentsOf: hidden.metaURL)) as! [String: Any]
    #expect(openMeta["private"] == nil)   // absent, not `false`: an old worker sees the usual meta
    #expect(hiddenMeta["private"] as? Bool == true)
    #expect(hiddenMeta["title"] as? String == "")
}

@Test func createAvoidsCollision() throws {
    let store = SessionStore(root: try tempRoot())
    let a = try store.create(app: "manual", title: "", participants: [], now: noon)
    let b = try store.create(app: "manual", title: "", participants: [], now: noon)
    #expect(a.id == "2026-09-15-1430")
    #expect(b.id == "2026-09-15-1430-1")
}

@Test func stopMarkerAndStateReading() throws {
    let store = SessionStore(root: try tempRoot())
    let s = try store.create(app: "manual", title: "", participants: [], now: noon)
    #expect(s.notePath() == nil)
    #expect(s.status() == nil)
    try "{\"status\": \"recording\", \"note_path\": \"/v/MTG-x.md\"}".write(to: s.stateURL, atomically: true, encoding: .utf8)
    #expect(s.notePath() == "/v/MTG-x.md")
    #expect(s.status() == "recording")
    try s.requestStop()
    #expect(FileManager.default.fileExists(atPath: s.stopURL.path))
    try s.requestStop()   // idempotent
}

@Test func reopenExisting() throws {
    let root = try tempRoot()
    let s = try SessionStore(root: root).create(app: "manual", title: "", participants: [], now: noon)
    let again = SessionStore(root: root).open(id: s.id)
    #expect(again?.url == s.url)
    #expect(SessionStore(root: root).open(id: "nope") == nil)
}
