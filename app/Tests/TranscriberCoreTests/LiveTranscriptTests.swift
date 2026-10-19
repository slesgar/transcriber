import Foundation
import Testing
@testable import TranscriberCore

private func tempSession() throws -> SessionDir {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return SessionDir(url: dir)
}

private func writeState(_ dir: SessionDir, _ state: [String: Any]) throws {
    try JSONSerialization.data(withJSONObject: state).write(to: dir.stateURL)
}

private let raw = """
---
type: raw
source: local
---

# Синк по витринам — 2026-09-15 14:30

Локальная запись (Толк). Участники: Иван Сидоров. Статус: идёт запись. «Me» — Владелец (микрофон).

## Полный транскрипт

Me: Начнём с витрины заказов.

Speaker 1: Да, там вчера был сбой.

— 05:00 —

Me: Понял.

"""

@Test func bodyIsEverythingAfterTranscriptHeading() {
    let body = LiveTranscript.body(ofRaw: raw)
    #expect(body.hasPrefix("Me: Начнём с витрины заказов."))
    #expect(body.contains("Speaker 1: Да, там вчера был сбой."))
    #expect(body.contains("— 05:00 —"))
    #expect(body.hasSuffix("Me: Понял."))   // trailing blank lines trimmed
    #expect(!body.contains("Статус:"))
}

@Test func bodyIsEmptyBeforeFirstLine() {
    let header = raw.components(separatedBy: "## Полный транскрипт")[0] + "## Полный транскрипт\n\n"
    #expect(LiveTranscript.body(ofRaw: header) == "")
}

@Test func bodyWithoutHeadingIsEmpty() {
    #expect(LiveTranscript.body(ofRaw: "# Что-то другое\n\nтекст") == "")
}

@Test func readWaitsForWorkerThenReturnsText() throws {
    let dir = try tempSession()
    defer { try? FileManager.default.removeItem(at: dir.url) }
    #expect(LiveTranscript.read(dir) == .waiting)            // no state.json yet

    try writeState(dir, ["status": "recording"])             // worker started, no raw yet
    #expect(LiveTranscript.read(dir) == .waiting)

    let rawURL = dir.url.appendingPathComponent("raw.md")
    try writeState(dir, ["status": "recording", "raw_path": rawURL.path])
    #expect(LiveTranscript.read(dir) == .waiting)            // path written, file not yet

    try raw.write(to: rawURL, atomically: true, encoding: .utf8)
    #expect(dir.rawPath() == rawURL.path)
    guard case .text(let body) = LiveTranscript.read(dir) else {
        Issue.record("expected text")
        return
    }
    #expect(body.hasPrefix("Me: Начнём"))
}

@Test func readOfHeaderOnlyRawIsWaiting() throws {
    let dir = try tempSession()
    defer { try? FileManager.default.removeItem(at: dir.url) }
    let rawURL = dir.url.appendingPathComponent("raw.md")
    try "# Синк\n\n## Полный транскрипт\n\n".write(to: rawURL, atomically: true, encoding: .utf8)
    try writeState(dir, ["raw_path": rawURL.path])
    #expect(LiveTranscript.read(dir) == .waiting)
}

@Test func bodyFindsHeadingInCrlfRaw() {
    let crlf = "# Синк\r\n\r\n## Полный транскрипт\r\n\r\nMe: Привет.\r\n"
    #expect(LiveTranscript.body(ofRaw: crlf) == "Me: Привет.")
}

@Test func appendixOnlyWhenTextGrewAtTheEnd() {
    #expect(LiveTranscript.appendix(from: "Me: А.", to: "Me: А.\n\nSpeaker 1: Б.") == "\n\nSpeaker 1: Б.")
    #expect(LiveTranscript.appendix(from: "Me: А.", to: "Me: А.") == nil)            // nothing new
    #expect(LiveTranscript.appendix(from: "Me: А.", to: "Speaker 1: А.") == nil)     // rewritten
    #expect(LiveTranscript.appendix(from: "", to: "Me: А.") == "Me: А.")
}

@Test func followerAnswersOnceThenOnlyOnChange() throws {
    let dir = try tempSession()
    defer { try? FileManager.default.removeItem(at: dir.url) }
    let follower = LiveTranscriptFollower(session: dir)
    #expect(follower.poll() == .waiting)       // first poll shows the placeholder
    #expect(follower.poll() == nil)            // still nothing — no news

    let rawURL = dir.url.appendingPathComponent("raw.md")
    try writeState(dir, ["raw_path": rawURL.path])
    try "# Синк\n\n## Полный транскрипт\n\nMe: А.\n".write(to: rawURL, atomically: true, encoding: .utf8)
    #expect(follower.poll() == .text("Me: А."))
    #expect(follower.poll() == nil)            // same mtime and size

    // the path is pinned: a later state.json without it does not lose the Raw
    try writeState(dir, ["status": "recording"])
    try "# Синк\n\n## Полный транскрипт\n\nMe: А.\n\nSpeaker 1: Б.\n".write(to: rawURL, atomically: true, encoding: .utf8)
    #expect(follower.poll() == .text("Me: А.\n\nSpeaker 1: Б."))
}
