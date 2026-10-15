import Foundation

/// Append-only int16 mono PCM file. `write` buffers; every `flushEvery` samples (default one second
/// at 16 kHz) the buffer is written and fsync'ed, so a lid close loses at most one second.
public final class PcmWriter {
    private let handle: FileHandle
    private let flushEvery: Int
    private var buffer: [Int16] = []
    private var closed = false
    public private(set) var samplesWritten = 0

    public init(url: URL, flushEvery: Int = 16_000) throws {
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        self.flushEvery = flushEvery
    }

    public func write(_ samples: [Int16]) {
        guard !closed else { return }
        buffer.append(contentsOf: samples)
        if buffer.count >= flushEvery {
            flush()
        }
    }

    public func flush() {
        guard !closed else { return }
        guard !buffer.isEmpty else { return }
        let data = buffer.withUnsafeBufferPointer { Data(buffer: $0) }
        handle.write(data)
        fsync(handle.fileDescriptor)
        samplesWritten += buffer.count
        buffer.removeAll(keepingCapacity: true)
    }

    public func close() {
        guard !closed else { return }
        flush()
        try? handle.close()
        closed = true
    }
}
