import AppKit
import Testing
@testable import Transcriber

/// A MacBook screen with the menu bar taken off the top, as `visibleFrame` reports it.
private let laptop = NSRect(x: 0, y: 0, width: 1470, height: 876)
private let card = NSSize(width: PromptPanel.width, height: PromptPanel.height)

@Suite("PromptPanel placement")
struct PromptPanelLayoutTests {
    @Test("top-right target fits and is left where it is")
    func fits() {
        let target = NSPoint(x: laptop.maxX - card.width - 16, y: laptop.maxY - card.height - 16)
        let origin = PromptPanel.clampedOrigin(size: card, target: target, in: laptop, margin: 16)
        #expect(origin == target)
    }

    @Test("a target past the right and top edges is pulled back inside the margin")
    func overflowsRightAndTop() {
        let target = NSPoint(x: laptop.maxX - 40, y: laptop.maxY - 10)
        let origin = PromptPanel.clampedOrigin(size: card, target: target, in: laptop, margin: 16)
        #expect(origin.x == laptop.maxX - 16 - card.width)
        #expect(origin.y == laptop.maxY - 16 - card.height)
        #expect(origin.x + card.width <= laptop.maxX - 16)
        #expect(origin.y + card.height <= laptop.maxY - 16)
    }

    @Test("a target past the left and bottom edges is pushed in")
    func overflowsLeftAndBottom() {
        let origin = PromptPanel.clampedOrigin(size: card, target: NSPoint(x: -300, y: -300),
                                               in: laptop, margin: 16)
        #expect(origin == NSPoint(x: laptop.minX + 16, y: laptop.minY + 16))
    }

    /// A secondary display sits at a non-zero origin; clamping has to respect it rather than
    /// assume the coordinate space starts at zero — this is the two-display bug.
    @Test("clamping follows a display whose frame does not start at the origin")
    func secondaryDisplay() {
        let external = NSRect(x: 1470, y: 300, width: 1920, height: 1055)
        let target = NSPoint(x: external.maxX + 500, y: external.maxY + 500)
        let origin = PromptPanel.clampedOrigin(size: card, target: target, in: external, margin: 16)
        #expect(origin.x == external.maxX - 16 - card.width)
        #expect(origin.y == external.maxY - 16 - card.height)
        #expect(origin.x >= external.minX + 16)
        #expect(origin.y >= external.minY + 16)
    }

    /// Nothing sane fits on a screen narrower than the card, but returning an origin outside the
    /// frame would be worse than pinning it to the margin.
    @Test("a screen smaller than the card pins the origin to the bottom-left margin")
    func tooSmall() {
        let tiny = NSRect(x: 0, y: 0, width: 200, height: 100)
        let origin = PromptPanel.clampedOrigin(size: card, target: NSPoint(x: 180, y: 90),
                                               in: tiny, margin: 16)
        #expect(origin == NSPoint(x: 16, y: 16))
    }
}

@Suite("FloatingButton elapsed time")
struct FloatingButtonElapsedTests {
    @Test("minutes and seconds")
    func minutesSeconds() {
        let start = Date(timeIntervalSince1970: 1_000)
        #expect(FloatingButton.elapsed(since: start, now: start.addingTimeInterval(65)) == "01:05")
        #expect(FloatingButton.elapsed(since: start, now: start) == "00:00")
    }

    @Test("hours once a meeting runs that long")
    func hours() {
        let start = Date(timeIntervalSince1970: 1_000)
        #expect(FloatingButton.elapsed(since: start, now: start.addingTimeInterval(3_725)) == "1:02:05")
    }

    @Test("no start time")
    func missing() {
        #expect(FloatingButton.elapsed(since: nil) == "—")
    }
}
