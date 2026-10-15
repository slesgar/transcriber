import Foundation
import Testing
@testable import TranscriberCore

@Test func writerAppendsLittleEndianInt16AndFlushes() throws {
    let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".pcm")
    let w = try PcmWriter(url: file, flushEvery: 4)
    w.write([1, -2, 3])
    #expect((try? Data(contentsOf: file))?.count ?? 0 == 0)     // below threshold, still buffered
    w.write([4, 5])                                              // crosses threshold → flushed
    #expect(try Data(contentsOf: file).count == 10)
    w.write([6])
    w.close()
    let data = try Data(contentsOf: file)
    #expect(data.count == 12)
    let samples = data.withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
    #expect(samples == [1, -2, 3, 4, 5, 6])
}

@Test func writerCreatesFileImmediately() throws {
    let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".pcm")
    let w = try PcmWriter(url: file)
    #expect(FileManager.default.fileExists(atPath: file.path))
    w.close()
}

@Test func writeAndFlushAfterCloseAreNoOps() throws {
    let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".pcm")
    let w = try PcmWriter(url: file, flushEvery: 100)
    w.write([1, 2, 3])
    w.close()
    let dataAfterFirstClose = try Data(contentsOf: file)
    #expect(dataAfterFirstClose.count == 6)   // first samples flushed on close

    // A late Core Audio callback arriving after close() must be a no-op, not a crash.
    w.write([4, 5, 6])
    w.flush()
    w.close()   // idempotent

    let data = try Data(contentsOf: file)
    #expect(data.count == 6)
    let samples = data.withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
    #expect(samples == [1, 2, 3])
}
