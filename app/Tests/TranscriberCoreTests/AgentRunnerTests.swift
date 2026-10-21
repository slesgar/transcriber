import Foundation
import Testing
@testable import TranscriberCore

/// Агент — любая команда оболочки: вопрос уходит на stdin, ответ — stdout. Тесты гоняют `cat`,
/// `pwd`, `sleep` и `false` вместо настоящих агентов.
private func run(_ command: String, prompt: String = "", cwd: URL? = nil, timeout: TimeInterval = 5) async -> AgentResult {
    await withCheckedContinuation { cont in
        AgentRunner.run(command: command, prompt: prompt, cwd: cwd, timeout: timeout,
                        queue: DispatchQueue(label: "agent-test")) { cont.resume(returning: $0) }
    }
}

@Test func agentGetsPromptOnStdinAndAnswersOnStdout() async {
    let r = await run("cat", prompt: "Привет, агент")
    #expect(r == .answer("Привет, агент"))
}

@Test func agentRunsInGivenFolder() async {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    guard case .answer(let out) = await run("pwd -P", cwd: dir) else { Issue.record("no answer"); return }
    #expect(URL(fileURLWithPath: out).resolvingSymlinksInPath() == dir.resolvingSymlinksInPath())
}

@Test func agentFailureCarriesExitCodeAndStderr() async {
    let r = await run("echo сломалось >&2; exit 3")
    #expect(r == .failed(code: 3, message: "сломалось"))
}

@Test func emptyAnswerIsAFailure() async {
    #expect(await run("true") == .failed(code: 0, message: "агент ничего не ответил"))
}

@Test func slowAgentTimesOut() async {
    _ = ShellPath.discovered   // the app warms it at launch; here it would eat the timeout budget
    let started = Date()
    #expect(await run("sleep 5", timeout: 0.5) == .timedOut)
    #expect(Date().timeIntervalSince(started) < 4)
}

@Test func emptyCommandIsNotConfigured() async {
    #expect(await run("   ") == .notConfigured)
}

@Test func agentConfigReadsFileAndToleratesHalfBlocks() throws {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let url = dir.appendingPathComponent("app-config.json")
    #expect(AgentConfig.load(from: url) == .defaults)
    try #"{"agent": {"command": "agent-cli"}}"#.write(to: url, atomically: true, encoding: .utf8)
    #expect(AgentConfig.load(from: url) == AgentConfig(command: "agent-cli", contextDir: "", timeoutS: 180))
    try #"{"agent": 5}"#.write(to: url, atomically: true, encoding: .utf8)
    #expect(AgentConfig.load(from: url) == .defaults)
}

@Test func shellPathIsWhatFollowsTheMarker() {
    #expect(ShellPath.parse("conda says hi\n\n__TRANSCRIBER_PATH__/opt/x/bin:/usr/bin") == "/opt/x/bin:/usr/bin")
    #expect(ShellPath.parse("нет маркера") == nil)
    #expect(ShellPath.parse("\n__TRANSCRIBER_PATH__") == nil)
}

/// Агент, не дочитавший вопрос, не роняет приложение (SIGPIPE) — вопрос больше буфера канала.
@Test func agentThatIgnoresAHugePromptDoesNotKillUs() async {
    let prompt = String(repeating: "встреча ", count: 200_000)
    #expect(await run("echo готов", prompt: prompt) == .answer("готов"))
}

/// Внуки команды не держат вывод после таймаута: ответ «не успел» приходит вовремя.
@Test func timeoutStopsGrandchildrenToo() async {
    _ = ShellPath.discovered
    let started = Date()
    #expect(await run("(sleep 30; echo поздно) | cat", prompt: String(repeating: "x", count: 200_000), timeout: 0.5) == .timedOut)
    #expect(Date().timeIntervalSince(started) < 4)
}

@Test func mistypedAgentKeyCostsOnlyThatKey() throws {
    let data = Data(#"{"command": "agent-cli", "timeout_s": "90"}"#.utf8)
    #expect(try JSONDecoder().decode(AgentConfig.self, from: data) == AgentConfig(command: "agent-cli", contextDir: "", timeoutS: 180))
}
