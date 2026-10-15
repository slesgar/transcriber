import Foundation
import Testing
@testable import TranscriberCore

@Test func obsidianURLEncodesPath() {
    let url = Obsidian.openURL(path: "/Users/v/Vault/30 · Meetings/MTG-2026-09-15 1430 · Синк.md")
    #expect(url.scheme == "obsidian")
    #expect(url.host == "open")
    #expect(url.absoluteString.hasPrefix("obsidian://open?path=%2FUsers%2Fv%2FVault%2F30%20"))
    let back = URLComponents(url: url, resolvingAgainstBaseURL: false)!.queryItems!.first { $0.name == "path" }!.value
    #expect(back == "/Users/v/Vault/30 · Meetings/MTG-2026-09-15 1430 · Синк.md")
}
