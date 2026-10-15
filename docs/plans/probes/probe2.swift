import Foundation
import CoreAudio
import AudioToolbox

func getProp<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ def: T) -> T {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size); var v = def
    AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &v); return v
}
func uidOf(_ dev: AudioObjectID) -> String {
    let u: Unmanaged<CFString>? = getProp(dev, kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal, nil)
    return (u?.takeRetainedValue() as String?) ?? ""
}
let mic: AudioObjectID = getProp(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultInputDevice, kAudioObjectPropertyScopeGlobal, AudioObjectID(0))
print("default mic", mic, uidOf(mic))

let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
desc.name = "probe-tap"; desc.isPrivate = true; desc.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
print("tap", AudioHardwareCreateProcessTap(desc, &tapID), tapID)
let tapUID = desc.uuid.uuidString

let aggDesc: [String: Any] = [
    kAudioAggregateDeviceNameKey as String: "probe-agg",
    kAudioAggregateDeviceUIDKey as String: "ru.slesarev.transcriber.probe",
    kAudioAggregateDeviceIsPrivateKey as String: true,
    kAudioAggregateDeviceIsStackedKey as String: false,
    kAudioAggregateDeviceTapAutoStartKey as String: true,
    kAudioAggregateDeviceSubDeviceListKey as String: [[kAudioSubDeviceUIDKey as String: uidOf(mic)]],
    kAudioAggregateDeviceTapListKey as String: [[kAudioSubTapUIDKey as String: tapUID, kAudioSubTapDriftCompensationKey as String: true]],
]
var agg = AudioObjectID(kAudioObjectUnknown)
print("agg", AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &agg), agg)

// input stream layout of the aggregate
var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration, mScope: kAudioObjectPropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
var size: UInt32 = 0
AudioObjectGetPropertyDataSize(agg, &addr, 0, nil, &size)
let bufList = UnsafeMutablePointer<AudioBufferList>.allocate(capacity: Int(size))
AudioObjectGetPropertyData(agg, &addr, 0, nil, &size, bufList)
let abl = UnsafeMutableAudioBufferListPointer(bufList)
print("input buffers:", abl.map { "\($0.mNumberChannels)ch" })
let rate: Double = getProp(agg, kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, 0.0)
print("agg rate", rate)

final class Stats { var calls = 0; var frames = 0; var peaks: [Float] = [] }
let stats = Stats()
var procID: AudioDeviceIOProcID? = nil
let st = AudioDeviceCreateIOProcIDWithBlock(&procID, agg, nil) { (now, inData, inTime, outData, outTime) in
    let bl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    stats.calls += 1
    if stats.peaks.isEmpty { stats.peaks = [Float](repeating: 0, count: bl.count) }
    for (i, b) in bl.enumerated() {
        let n = Int(b.mDataByteSize) / 4
        if i == 0 { stats.frames += n / Int(max(b.mNumberChannels, 1)) }
        let p = b.mData!.assumingMemoryBound(to: Float.self)
        var peak: Float = 0
        for k in 0..<n { peak = max(peak, abs(p[k])) }
        stats.peaks[i] = max(stats.peaks[i], peak)
    }
}
print("ioproc", st)
print("start", AudioDeviceStart(agg, procID))
Thread.sleep(forTimeInterval: 3)
print("stop", AudioDeviceStop(agg, procID))
print("calls", stats.calls, "frames", stats.frames, "peaks per buffer", stats.peaks)
AudioDeviceDestroyIOProcID(agg, procID!)
AudioHardwareDestroyAggregateDevice(agg)
AudioHardwareDestroyProcessTap(tapID)
