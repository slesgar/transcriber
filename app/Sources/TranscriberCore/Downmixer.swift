import AVFoundation
import Foundation

public enum DownmixerError: Error { case badFormat, converterUnavailable }

/// Converts interleaved float32 audio at any rate/channel count to int16 mono 16 kHz.
/// Not thread-safe: call from one serial queue.
public final class Downmixer {
    public static let outputRate: Double = 16_000

    private let inFormat: AVAudioFormat
    private let outFormat: AVAudioFormat
    private let converter: AVAudioConverter
    private let ratio: Double
    private let log: Log

    public init(inputRate: Double, inputChannels: Int, log: Log = .shared) throws {
        self.log = log
        guard let inF = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: inputRate,
                                      channels: AVAudioChannelCount(inputChannels), interleaved: true),
              let outF = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Self.outputRate,
                                       channels: 1, interleaved: true)
        else { throw DownmixerError.badFormat }
        guard let conv = AVAudioConverter(from: inF, to: outF) else { throw DownmixerError.converterUnavailable }
        conv.downmix = true
        inFormat = inF
        outFormat = outF
        converter = conv
        ratio = Self.outputRate / inputRate
    }

    /// `interleaved` holds `frames * inputChannels` floats.
    public func process(interleaved: [Float], frames: Int) -> [Int16] {
        guard frames > 0 else { return [] }
        guard interleaved.count >= frames * Int(inFormat.channelCount) else {
            log.error("downmix: short input — \(interleaved.count) floats for \(frames) frames × \(inFormat.channelCount)ch")
            return []
        }
        guard let inBuf = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: AVAudioFrameCount(frames))
        else { return [] }
        inBuf.frameLength = AVAudioFrameCount(frames)
        interleaved.withUnsafeBufferPointer { src in
            let dst = inBuf.audioBufferList.pointee.mBuffers.mData!.assumingMemoryBound(to: Float.self)
            dst.update(from: src.baseAddress!, count: frames * Int(inFormat.channelCount))
        }
        return convert(input: inBuf, capacity: Int(Double(frames) * ratio) + 64)
    }

    /// Drains the converter's internal delay line. Call once at the end of a recording.
    public func finish() -> [Int16] {
        let out = convert(input: nil, capacity: 4096)
        converter.reset()
        return out
    }

    private func convert(input: AVAudioPCMBuffer?, capacity: Int) -> [Int16] {
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: AVAudioFrameCount(capacity)) else { return [] }
        var delivered = false
        var error: NSError?
        let status = converter.convert(to: outBuf, error: &error) { _, outStatus in
            if delivered || input == nil {
                outStatus.pointee = input == nil ? .endOfStream : .noDataNow
                return nil
            }
            delivered = true
            outStatus.pointee = .haveData
            return input
        }
        if status == .error {
            log.error("downmix: \(error?.localizedDescription ?? "unknown")")
            return []
        }
        let n = Int(outBuf.frameLength)
        let p = outBuf.audioBufferList.pointee.mBuffers.mData!.assumingMemoryBound(to: Int16.self)
        return Array(UnsafeBufferPointer(start: p, count: n))
    }
}
