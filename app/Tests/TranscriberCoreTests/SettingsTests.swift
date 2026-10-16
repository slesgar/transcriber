import Foundation
import Testing
@testable import TranscriberCore

/// Каждый тест — свой временный каталог: рабочий каталог данных не трогаем.
private func tempDir() -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
}

@Test func yamlEditorKeepsCommentsAndUnknownKeys() {
    let text = "# шапка\nowner_name: Owner   # кто\nspeaker_similarity: 0.3\n"
    let out = SettingsStore.settingYAML(text, values: [("owner_name", "Анна"), ("vault_root", "~/V")])
    #expect(out.hasPrefix("# шапка\n")); #expect(out.contains("owner_name: \"Анна\"")); #expect(out.contains("speaker_similarity: 0.3"))
    #expect(out.hasSuffix("vault_root: \"~/V\"\n"))
}

@Test func yamlEditorIgnoresKeyInsideComment() {
    let text = "# owner_name: старое\nspeaker_similarity: 0.3"
    let out = SettingsStore.settingYAML(text, values: [("owner_name", "Анна")])
    #expect(out == "# owner_name: старое\nspeaker_similarity: 0.3\nowner_name: \"Анна\"\n")
}

@Test func privacyEditorReplacesOnlyPrivacyFields() {
    let text = #"{"_comment": "c", "privacy": {"apps": ["com.example.chat"], "windows": [], "workApps": ["kontur.talk"], "workHosts": []}, "recon": {"enabled": true, "seconds": 180}}"#
    let out = SettingsStore.settingPrivacy(text, windows: [PrivateWindow(weekday: 3, from: "12:00", to: "12:50")], apps: [], workHosts: ["talk.example.com"])!
    let obj = try! JSONSerialization.jsonObject(with: out.data(using: .utf8)!) as! [String: Any]
    #expect((obj["recon"] as! [String: Any])["seconds"] as! Int == 180); #expect(obj["_comment"] as! String == "c")
    let privacy = obj["privacy"] as! [String: Any]
    #expect(privacy["workHosts"] as! [String] == ["talk.example.com"]); #expect(privacy["workApps"] as! [String] == ["kontur.talk"])
    #expect((privacy["windows"] as! [[String: Any]]).count == 1)
    #expect(out.contains(#""recon": {"#)); #expect(out.contains(#""seconds": 180"#)); #expect(!out.contains(#"" : "#))
}

@Test func privacyEditorKeepsColonSpaceInsideValues() {
    let out = SettingsStore.settingPrivacy(#"{"_comment": "a\" : b"}"#, windows: [], apps: [], workHosts: [])!
    let obj = try! JSONSerialization.jsonObject(with: out.data(using: .utf8)!) as! [String: Any]
    #expect(obj["_comment"] as! String == "a\" : b")
}

@Test func validationNamesEmptyRequiredFields() {
    var s = Settings(ownerName: "Анна", ownerTalkName: "", vaultRoot: "~/V", privateDir: "~/P",
                     workHosts: [], privateWindows: [], privateApps: [])
    #expect(s.validationErrors().isEmpty)
    s.ownerName = "  "; s.vaultRoot = ""; s.privateDir = ""
    #expect(s.validationErrors() == ["Имя владельца", "Вольт (путь)", "Папка приватных записей"])
}

@Test func listFieldSplitsOnCommasAndWhitespace() {
    #expect(Settings.splitList(" talk.example.com,meet.example.com  com.example.chat ,, \n") == ["talk.example.com", "meet.example.com", "com.example.chat"])
    #expect(Settings.splitList("  ").isEmpty)
}

@Test func privacyEditorRejectsNonObject() {
    #expect(SettingsStore.settingPrivacy("[1, 2]", windows: [], apps: [], workHosts: []) == nil)
    #expect(SettingsStore.settingPrivacy("не json", windows: [], apps: [], workHosts: []) == nil)
}

@Test func saveThenLoadRoundTrips() throws {
    let dir = tempDir()
    let s = Settings(ownerName: "Анна", ownerTalkName: "Анна Петрова", vaultRoot: "~/V", privateDir: "~/P",
                     workHosts: ["talk.example.com"], privateWindows: [PrivateWindow(weekday: 3, from: "12:00", to: "12:50")], privateApps: ["com.example.chat"])
    try SettingsStore.save(s, appConfig: dir.appendingPathComponent("app-config.json"), workerConfig: dir.appendingPathComponent("config.yaml"))
    #expect(SettingsStore.load(appConfig: dir.appendingPathComponent("app-config.json"), workerConfig: dir.appendingPathComponent("config.yaml")) == s)
    #expect(!(try String(contentsOf: dir.appendingPathComponent("config.yaml")).contains("owner_thoughts_heading")))
}

/// Заголовок мыслей владельца правится только в файле: вольт ищет его дословно.
@Test func saveLeavesThoughtsHeadingUntouched() throws {
    let dir = tempDir()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let json = dir.appendingPathComponent("app-config.json"), yaml = dir.appendingPathComponent("config.yaml")
    try "owner_name: Owner\nowner_thoughts_heading: \"## Мысли владельца\"\n".write(to: yaml, atomically: true, encoding: .utf8)
    var s = SettingsStore.load(appConfig: json, workerConfig: yaml)
    s.ownerName = "Анна"
    try SettingsStore.save(s, appConfig: json, workerConfig: yaml)
    let text = try String(contentsOf: yaml)
    #expect(text.contains("owner_thoughts_heading: \"## Мысли владельца\"")); #expect(!text.contains("Мысли Анна"))
}

@Test func loadWithoutFilesGivesNeutralDefaults() {
    let dir = tempDir()
    let s = SettingsStore.load(appConfig: dir.appendingPathComponent("app-config.json"),
                               workerConfig: dir.appendingPathComponent("config.yaml"))
    #expect(s == Settings(ownerName: "Owner", ownerTalkName: "", vaultRoot: "~/Documents/Vault",
                          privateDir: "~/Documents/Transcriber Private", workHosts: [], privateWindows: [], privateApps: []))
}

@Test func loadReadsUnquotedAndQuotedValues() throws {
    let dir = tempDir()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let yaml = dir.appendingPathComponent("config.yaml")
    try "vault_root: ~/Notes/Vault\nowner_name: Owner   # кто\nowner_talk_name: \"Анна Петрова\"\n"
        .write(to: yaml, atomically: true, encoding: .utf8)
    let s = SettingsStore.load(appConfig: dir.appendingPathComponent("app-config.json"), workerConfig: yaml)
    #expect(s.vaultRoot == "~/Notes/Vault")
    #expect(s.ownerName == "Owner")
    #expect(s.ownerTalkName == "Анна Петрова")
}

@Test func saveKeepsForeignJSONMembersAndYAMLLines() throws {
    let dir = tempDir()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let json = dir.appendingPathComponent("app-config.json"), yaml = dir.appendingPathComponent("config.yaml")
    try #"{"_comment": "c", "privacy": {"apps": [], "windows": [], "workApps": ["kontur.talk"], "workHosts": []}, "recon": {"enabled": true}, "talk": {"x": 1}}"#
        .write(to: json, atomically: true, encoding: .utf8)
    try "# шапка\nspeaker_similarity: 0.3\nowner_name: Owner\n".write(to: yaml, atomically: true, encoding: .utf8)
    var s = SettingsStore.load(appConfig: json, workerConfig: yaml)
    s.ownerName = "Анна"
    try SettingsStore.save(s, appConfig: json, workerConfig: yaml)
    let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: json)) as! [String: Any]
    #expect(obj["_comment"] as? String == "c"); #expect(obj["recon"] != nil); #expect(obj["talk"] != nil)
    #expect(((obj["privacy"] as! [String: Any])["workApps"] as! [String]) == ["kontur.talk"])
    let lines = try String(contentsOf: yaml).components(separatedBy: "\n")
    #expect(Array(lines.prefix(3)) == ["# шапка", "speaker_similarity: 0.3", "owner_name: \"Анна\""])
}

@Test func privateWindowLineRoundTrip() {
    #expect(PrivateWindow.parse(line: "3 12:00-12:50") == PrivateWindow(weekday: 3, from: "12:00", to: "12:50"))
    #expect(PrivateWindow(weekday: 3, from: "12:00", to: "12:50").line == "3 12:00-12:50")
    #expect(PrivateWindow.parse(line: "8 12:00-12:50") == nil); #expect(PrivateWindow.parse(line: "пн") == nil)
    #expect(PrivateWindow.parse(line: "  3   12:00 - 12:50 ") == PrivateWindow(weekday: 3, from: "12:00", to: "12:50"))
    #expect(PrivateWindow.parse(line: "3 25:00-12:50") == nil)
}

/// Неразборчивый `privacy` показан в окне как умолчания; сохранить их — стереть расписание владельца.
@Test func saveRefusesMalformedPrivacy() throws {
    let dir = tempDir()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let json = dir.appendingPathComponent("app-config.json"), yaml = dir.appendingPathComponent("config.yaml")
    let broken = #"{"privacy": {"windows": "3 12:00-12:50", "apps": []}, "recon": {"enabled": true}}"#
    try broken.write(to: json, atomically: true, encoding: .utf8)
    let s = SettingsStore.load(appConfig: json, workerConfig: yaml)
    #expect(throws: SettingsError.self) { try SettingsStore.save(s, appConfig: json, workerConfig: yaml) }
    #expect(try String(contentsOf: json) == broken)
    #expect(!FileManager.default.fileExists(atPath: yaml.path))
    #expect(SettingsStore.privacyIsMalformed(#"{"privacy": 5}"#))
    #expect(!SettingsStore.privacyIsMalformed(#"{"recon": {}}"#))
    #expect(!SettingsStore.privacyIsMalformed(#"{"privacy": {"windows": [], "apps": []}}"#))
}
