import AudioToolbox
import CoreAudio
import Foundation

public enum CaptureError: Error, CustomStringConvertible {
    case coreAudio(String, OSStatus)
    case noInputDevice
    case unexpectedStreams(Int)

    public var description: String {
        switch self {
        case .coreAudio(let what, let status): return "\(what) failed: OSStatus \(status)"
        case .noInputDevice: return "no default input device"
        case .unexpectedStreams(let n): return "aggregate device has \(n) input buffers, expected 2 (mic, tap)"
        }
    }
}

/// One IOProc cycle's worth of audio from both inputs, deinterleaved by role.
public struct CaptureChunk {
    public let mic: [Float]     // mono
    public let sys: [Float]     // interleaved, `sysChannels` channels
    public let frames: Int
}

/// Captures the default microphone and a global process tap (all processes except ours)
/// through a private aggregate device. Delivers chunks on a serial queue.
public final class TapCapture {
    public private(set) var sampleRate: Double = 48_000
    public private(set) var sysChannels: Int = 2

    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private let queue = DispatchQueue(label: "transcriber.capture", qos: .userInitiated)
    private var handler: ((CaptureChunk) -> Void)?
    private var warnedShortBuffers = false   // capture queue only

    public init() {}

    public func start(onChunk: @escaping (CaptureChunk) -> Void) throws {
        handler = onChunk
        let system = AudioObjectID(kAudioObjectSystemObject)
        let mic: AudioObjectID = CoreAudioProps.get(system, kAudioHardwarePropertyDefaultInputDevice, default: AudioObjectID(0))
        guard mic != 0 else { throw CaptureError.noInputDevice }
        let micUID = CoreAudioProps.getString(mic, kAudioDevicePropertyDeviceUID)

        let excluded = AudioProcesses.ownProcessObject().map { [$0] } ?? []
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
        desc.name = "transcriber"
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        var tap = AudioObjectID(kAudioObjectUnknown)
        var status = AudioHardwareCreateProcessTap(desc, &tap)
        guard status == noErr else { throw CaptureError.coreAudio("AudioHardwareCreateProcessTap", status) }
        tapID = tap

        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "transcriber-agg",
            kAudioAggregateDeviceUIDKey as String: "ru.slesarev.transcriber.agg",
            kAudioAggregateDeviceIsPrivateKey as String: true,
            kAudioAggregateDeviceIsStackedKey as String: false,
            kAudioAggregateDeviceTapAutoStartKey as String: true,
            kAudioAggregateDeviceSubDeviceListKey as String: [[kAudioSubDeviceUIDKey as String: micUID]],
            kAudioAggregateDeviceTapListKey as String: [[
                kAudioSubTapUIDKey as String: desc.uuid.uuidString,
                kAudioSubTapDriftCompensationKey as String: true,
            ]],
        ]
        var agg = AudioObjectID(kAudioObjectUnknown)
        status = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &agg)
        guard status == noErr else {
            teardown()
            throw CaptureError.coreAudio("AudioHardwareCreateAggregateDevice", status)
        }
        aggregateID = agg
        sampleRate = CoreAudioProps.get(agg, kAudioDevicePropertyNominalSampleRate, default: 48_000.0)

        // Input layout: sub-devices first (mic, 1 buffer), then taps (1 buffer, stereo interleaved).
        let channels = inputBufferChannels(agg)
        guard channels.count == 2 else {
            teardown()
            throw CaptureError.unexpectedStreams(channels.count)
        }
        sysChannels = channels[1]
        Log.shared.info("capture: rate \(sampleRate), mic \(channels[0])ch, tap \(channels[1])ch")
        // Which devices the session actually started on: a 24 kHz rate is a Bluetooth headset, and
        // when it vanishes mid-meeting the next line in the log should say what it was.
        let input = AudioDevices.defaultInput()
        let output = AudioDevices.defaultOutput()
        Log.shared.info("capture: input '\(AudioDevices.name(of: input))' \(Int(sampleRate)) Hz, output '\(AudioDevices.name(of: output))'")

        var proc: AudioDeviceIOProcID?
        status = AudioDeviceCreateIOProcIDWithBlock(&proc, agg, queue) { [weak self] _, inData, _, _, _ in
            self?.deliver(inData)
        }
        guard status == noErr, let p = proc else {
            teardown()
            throw CaptureError.coreAudio("AudioDeviceCreateIOProcIDWithBlock", status)
        }
        procID = p
        status = AudioDeviceStart(agg, p)
        guard status == noErr else {
            teardown()
            throw CaptureError.coreAudio("AudioDeviceStart", status)
        }
    }

    public func stop() {
        if let p = procID, aggregateID != kAudioObjectUnknown {
            check("AudioDeviceStop", AudioDeviceStop(aggregateID, p))
        }
        teardown()
        queue.sync {}   // let the last delivered chunk finish
        handler = nil
    }

    private func teardown() {
        if let p = procID, aggregateID != kAudioObjectUnknown {
            check("AudioDeviceDestroyIOProcID", AudioDeviceDestroyIOProcID(aggregateID, p))
        }
        procID = nil
        if aggregateID != kAudioObjectUnknown {
            check("AudioHardwareDestroyAggregateDevice", AudioHardwareDestroyAggregateDevice(aggregateID))
            aggregateID = kAudioObjectUnknown
        }
        if tapID != kAudioObjectUnknown {
            check("AudioHardwareDestroyProcessTap", AudioHardwareDestroyProcessTap(tapID))
            tapID = kAudioObjectUnknown
        }
    }

    /// Teardown is best-effort by nature, but a silent non-zero status here is exactly how a
    /// leaked tap or aggregate device begins — so every call reports itself.
    private func check(_ call: String, _ status: OSStatus) {
        guard status != noErr else { return }
        Log.shared.warn("capture teardown: \(call) returned OSStatus \(status)")
    }

    private func inputBufferChannels(_ device: AudioObjectID) -> [Int] {
        var addr = CoreAudioProps.address(kAudioDevicePropertyStreamConfiguration, scope: kAudioObjectPropertyScopeInput)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &addr, 0, nil, &size) == noErr else { return [] }
        let list = UnsafeMutablePointer<AudioBufferList>.allocate(capacity: Int(size))
        defer { list.deallocate() }
        guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, list) == noErr else { return [] }
        return UnsafeMutableAudioBufferListPointer(list).map { Int($0.mNumberChannels) }
    }

    /// Runs on `queue` (the IOProc dispatch queue): copy out and hand over.
    private func deliver(_ inData: UnsafePointer<AudioBufferList>) {
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
        guard let handler else { return }
        guard buffers.count >= 2 else {
            if !warnedShortBuffers {
                warnedShortBuffers = true
                Log.shared.warn("capture: input device disappeared, mic track stalled")
            }
            return
        }
        let micBuf = buffers[0]
        let sysBuf = buffers[1]
        guard let micPtr = micBuf.mData, let sysPtr = sysBuf.mData else { return }
        let micCount = Int(micBuf.mDataByteSize) / MemoryLayout<Float>.size
        let sysCount = Int(sysBuf.mDataByteSize) / MemoryLayout<Float>.size
        let mic = Array(UnsafeBufferPointer(start: micPtr.assumingMemoryBound(to: Float.self), count: micCount))
        let sys = Array(UnsafeBufferPointer(start: sysPtr.assumingMemoryBound(to: Float.self), count: sysCount))
        let frames = micCount / max(Int(micBuf.mNumberChannels), 1)
        handler(CaptureChunk(mic: mic, sys: sys, frames: frames))
    }
}
