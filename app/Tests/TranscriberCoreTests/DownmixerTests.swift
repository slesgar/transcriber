import Foundation
import Testing
@testable import TranscriberCore

private func sine(freq: Double, rate: Double, seconds: Double, channels: Int, amp: Float = 0.5) -> [Float] {
    let n = Int(rate * seconds)
    var out = [Float](repeating: 0, count: n * channels)
    for i in 0..<n {
        let v = amp * Float(sin(2 * .pi * freq * Double(i) / rate))
        for c in 0..<channels { out[i * channels + c] = v }
    }
    return out
}

/// Keeps the suite out of ~/Library/Logs/transcriber-app.log.
private func tempLog() -> Log {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    return Log(file: dir.appendingPathComponent("downmixer-test.log"))
}

private func zeroCrossings(_ x: [Int16]) -> Int {
    var count = 0
    for i in 1..<x.count where (x[i - 1] < 0) != (x[i] < 0) { count += 1 }
    return count
}

@Test func stereo48kToMono16kKeepsPitch() throws {
    let d = try Downmixer(inputRate: 48_000, inputChannels: 2, log: tempLog())
    let input = sine(freq: 440, rate: 48_000, seconds: 1.0, channels: 2)
    var out: [Int16] = []
    // feed in 512-frame chunks like the IOProc does
    let frames = input.count / 2
    var i = 0
    while i < frames {
        let n = min(512, frames - i)
        out += d.process(interleaved: Array(input[(i * 2)..<((i + n) * 2)]), frames: n)
        i += n
    }
    out += d.finish()
    #expect(abs(out.count - 16_000) < 200)
    let zc = zeroCrossings(out)
    #expect(abs(zc - 880) < 40)                 // 440 Hz → 880 crossings per second
    #expect(out.map { abs(Int($0)) }.max()! > 12_000)   // amplitude 0.5 → ~16384
}

@Test func mono44kToMono16k() throws {
    let d = try Downmixer(inputRate: 44_100, inputChannels: 1, log: tempLog())
    let input = sine(freq: 1000, rate: 44_100, seconds: 0.5, channels: 1)
    let out = d.process(interleaved: input, frames: input.count) + d.finish()
    #expect(abs(out.count - 8_000) < 200)
    #expect(abs(zeroCrossings(out) - 1000) < 60)
}

@Test func shortInputIsDroppedNotRead() throws {
    let d = try Downmixer(inputRate: 48_000, inputChannels: 2, log: tempLog())
    #expect(d.process(interleaved: [Float](repeating: 0.1, count: 100), frames: 512).isEmpty)
}
