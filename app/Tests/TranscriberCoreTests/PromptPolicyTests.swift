import Testing
@testable import TranscriberCore

@Test func unansweredNonPrivateCallStarts() {
    #expect(UnansweredPrompt.action(isPrivate: false) == .start)
}

@Test func unansweredPrivateCallDeclines() {
    #expect(UnansweredPrompt.action(isPrivate: true) == .decline)
}

@Test func meetingCardWaitsThirtySeconds() {
    #expect(PromptTimeouts.meeting == 30)
    #expect(PromptTimeouts.privateMeeting == 90)
}

@Test func stopReasonsReadInRussian() {
    #expect(StopReason.manual.rawValue == "вручную")
    #expect(StopReason.meetingEnded.rawValue == "встреча закончилась")
}
