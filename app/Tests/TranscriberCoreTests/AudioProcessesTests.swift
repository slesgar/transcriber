import Testing
@testable import TranscriberCore

@Test func ownBundleIDMatchesItselfAndHelpersOnly() {
    #expect(AudioProcesses.isOwn("com.acme.transcriber", own: "com.acme.transcriber"))
    #expect(AudioProcesses.isOwn("com.acme.transcriber.helper", own: "com.acme.transcriber"))
    #expect(!AudioProcesses.isOwn("com.acme.transcriberx", own: "com.acme.transcriber"))
    #expect(!AudioProcesses.isOwn("org.example.other", own: "com.acme.transcriber"))
    #expect(!AudioProcesses.isOwn("", own: "com.acme.transcriber"))
}
