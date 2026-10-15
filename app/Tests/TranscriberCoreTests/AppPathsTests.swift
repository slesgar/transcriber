import Foundation
import Testing
@testable import TranscriberCore

/// Тесты трогают переменную окружения процесса, поэтому каждый возвращает её как было.
private func withEnvironment(_ value: String?, _ body: () -> Void) {
    let key = AppPaths.dataDirEnvironment
    let saved = getenv(key).map { String(cString: $0) }
    if let value { setenv(key, value, 1) } else { unsetenv(key) }
    defer { if let saved { setenv(key, saved, 1) } else { unsetenv(key) } }
    body()
}

/// Процессная переменная общая для всех тестов, поэтому эти идут строго по очереди.
@Suite(.serialized) struct AppPathsTests {
    @Test func dataDirFollowsEnvironment() {
        withEnvironment("/tmp/transcriber-test-data") {
            #expect(AppPaths.dataDir.path == "/tmp/transcriber-test-data")
            #expect(AppPaths.appConfig.lastPathComponent == "app-config.json")
            #expect(AppPaths.sessions.path == "/tmp/transcriber-test-data/sessions")
            #expect(AppPaths.workerConfig.path == "/tmp/transcriber-test-data/config.yaml")
        }
    }

    @Test func dataDirDefaultsToApplicationSupport() {
        withEnvironment(nil) {
            #expect(AppPaths.dataDir.path.hasSuffix("/Library/Application Support/Transcriber"))
        }
    }

    @Test func leadingTildeIsExpanded() {
        withEnvironment("~/transcriber-test-data") {
            #expect(AppPaths.dataDir.path == NSHomeDirectory() + "/transcriber-test-data")
        }
    }

    @Test func emptyEnvironmentFallsBackToDefault() {
        withEnvironment("") {
            #expect(AppPaths.dataDir.path.hasSuffix("/Library/Application Support/Transcriber"))
        }
    }
}
