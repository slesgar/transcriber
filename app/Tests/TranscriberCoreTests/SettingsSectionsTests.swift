import Foundation
import Testing
@testable import TranscriberCore

/// Тикет 16: разделы окна настроек — папки вольта, хранение аудио, наблюдение за Толком, агент.
private func tempDir() -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

@Test func loadWithoutFilesGivesNeutralSectionDefaults() {
    let dir = tempDir()
    let s = SettingsStore.load(appConfig: dir.appendingPathComponent("app-config.json"),
                               workerConfig: dir.appendingPathComponent("config.yaml"))
    #expect(s.rawSubdir == "Raw/Transcripts"); #expect(s.meetingsSubdir == "Meetings")
    #expect(s.keepAudio == false); #expect(s.talkEnabled == true)
    #expect(s.agent == AgentConfig.defaults); #expect(s.agent.command.isEmpty); #expect(s.agent.timeoutS == 180)
}

@Test func sectionValuesRoundTripAndKeepForeignMembers() throws {
    let dir = tempDir()
    let json = dir.appendingPathComponent("app-config.json"), yaml = dir.appendingPathComponent("config.yaml")
    try #"{"_comment": "c", "privacy": {"apps": [], "windows": [], "workApps": ["kontur.talk"], "workHosts": []}, "recon": {"enabled": true, "seconds": 180}, "talk": {"_comment": "t", "enabled": true}}"#
        .write(to: json, atomically: true, encoding: .utf8)
    try "# шапка\nkeep_audio: true   # аудио\nspeaker_similarity: 0.4\n".write(to: yaml, atomically: true, encoding: .utf8)
    var s = SettingsStore.load(appConfig: json, workerConfig: yaml)
    #expect(s.keepAudio == true)
    s.rawSubdir = "Notes/Raw"; s.meetingsSubdir = "Notes/Meetings"; s.keepAudio = false; s.talkEnabled = false
    s.agent = AgentConfig(command: "agent-cli --print", contextDir: "~/Notes", timeoutS: 90)
    try SettingsStore.save(s, appConfig: json, workerConfig: yaml)
    #expect(SettingsStore.load(appConfig: json, workerConfig: yaml) == s)

    let text = try String(contentsOf: yaml)
    #expect(text.contains("keep_audio: false\n")); #expect(text.contains("speaker_similarity: 0.4"))
    #expect(text.contains("raw_subdir: \"Notes/Raw\"")); #expect(text.hasPrefix("# шапка\n"))
    let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: json)) as! [String: Any]
    let talk = obj["talk"] as! [String: Any]
    #expect(talk["enabled"] as? Bool == false); #expect(talk["_comment"] as? String == "t")
    #expect((obj["recon"] as! [String: Any])["seconds"] as? Int == 180)
    let agent = obj["agent"] as! [String: Any]
    #expect(agent["command"] as? String == "agent-cli --print"); #expect(agent["context_dir"] as? String == "~/Notes")
    #expect(agent["timeout_s"] as? Int == 90); #expect(agent["_comment"] as? String != nil)
}

@Test func agentBlockKeepsItsOwnComment() throws {
    let text = #"{"agent": {"_comment": "мой", "command": "x", "context_dir": "", "timeout_s": 10}}"#
    let out = SettingsStore.settingAppConfig(text, settings: Settings(
        ownerName: "Анна", ownerTalkName: "", vaultRoot: "~/V", privateDir: "~/P",
        workHosts: [], privateWindows: [], privateApps: [], agent: AgentConfig(command: "y", contextDir: "", timeoutS: 10)))!
    let obj = try JSONSerialization.jsonObject(with: out.data(using: .utf8)!) as! [String: Any]
    #expect((obj["agent"] as! [String: Any])["_comment"] as? String == "мой")
    #expect((obj["agent"] as! [String: Any])["command"] as? String == "y")
}

@Test func validationRejectsAbsoluteSubdirsAndZeroTimeout() {
    var s = Settings(ownerName: "Анна", ownerTalkName: "", vaultRoot: "~/V", privateDir: "~/P",
                     workHosts: [], privateWindows: [], privateApps: [])
    #expect(s.validationErrors().isEmpty)
    s.rawSubdir = "/abs"; s.meetingsSubdir = "~/M"; s.agent.timeoutS = 0
    #expect(s.validationErrors() == ["Папка транскриптов — путь внутри вольта, без / и ~ в начале",
                                     "Папка заметок встреч — путь внутри вольта, без / и ~ в начале",
                                     "Сколько ждать ответа агента — больше нуля"])
    s.rawSubdir = ""
    #expect(s.validationErrors().first == "Папка транскриптов")
}

@Test func yamlLiteralIsWrittenUnquoted() {
    let out = SettingsStore.settingYAML("keep_audio: true\n", literals: [("keep_audio", "false"), ("x", "1")])
    #expect(out == "keep_audio: false\nx: 1\n")
}

@Test func folderStatusTellsExistingMissingAndFile() throws {
    let dir = tempDir()
    let file = dir.appendingPathComponent("f.txt")
    try "x".write(to: file, atomically: true, encoding: .utf8)
    #expect(FolderStatus.check(dir.path) == .exists)
    #expect(FolderStatus.check(dir.appendingPathComponent("nope").path) == .missing)
    #expect(FolderStatus.check(file.path) == .notFolder)
    #expect(FolderStatus.check("  ") == .empty)
    #expect(FolderStatus.resolve("~/x", inside: nil) == FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("x").path)
    #expect(FolderStatus.resolve("Raw/T", inside: "~/V") == FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("V/Raw/T").path)
}
