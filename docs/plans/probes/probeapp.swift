import Foundation
import CoreAudio
import AudioToolbox
import AVFoundation

let logPath = NSString(string: "~/Library/Logs/transcriber-probe.log").expandingTildeInPath
func log(_ s: String) {
    let line = "\(Date()) \(s)\n"
    if let h = FileHandle(forWritingAtPath: logPath) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
    else { try? line.write(toFile: logPath, atomically: true, encoding: .utf8) }
}
func getProp<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ def: T) -> T {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size); var v = def
    AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &v); return v
}
func uidOf(_ dev: AudioObjectID) -> String {
    let u: Unmanaged<CFString>? = getProp(dev, kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal, nil)
    return (u?.takeRetainedValue() as String?) ?? ""
}
log("probe start; mic auth status = \(AVCaptureDevice.authorizationStatus(for: .audio).rawValue)")
let sem = DispatchSemaphore(value: 0)
AVCaptureDevice.requestAccess(for: .audio) { ok in log("mic access granted: \(ok)"); sem.signal() }
sem.wait()

let mic: AudioObjectID = getProp(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultInputDevice, kAudioObjectPropertyScopeGlobal, AudioObjectID(0))
let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
desc.name = "transcriber-probe"; desc.isPrivate = true; desc.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
log("tap create: \(AudioHardwareCreateProcessTap(desc, &tapID)) id \(tapID)")
let aggDesc: [String: Any] = [
    kAudioAggregateDeviceNameKey as String: "transcriber-probe-agg",
    kAudioAggregateDeviceUIDKey as String: "ru.slesarev.transcriber.probe",
    kAudioAggregateDeviceIsPrivateKey as String: true,
    kAudioAggregateDeviceTapAutoStartKey as String: true,
    kAudioAggregateDeviceSubDeviceListKey as String: [[kAudioSubDeviceUIDKey as String: uidOf(mic)]],
    kAudioAggregateDeviceTapListKey as String: [[kAudioSubTapUIDKey as String: desc.uuid.uuidString, kAudioSubTapDriftCompensationKey as String: true]],
]
var agg = AudioObjectID(kAudioObjectUnknown)
log("agg create: \(AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &agg)) id \(agg)")
final class Stats { var peaks: [Float] = [] ; var calls = 0 }
let stats = Stats()
var procID: AudioDeviceIOProcID? = nil
let st = AudioDeviceCreateIOProcIDWithBlock(&procID, agg, nil) { (_, inData, _, _, _) in
    let bl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    stats.calls += 1
    if stats.peaks.isEmpty { stats.peaks = [Float](repeating: 0, count: bl.count) }
    for (i, b) in bl.enumerated() {
        let n = Int(b.mDataByteSize) / 4; let p = b.mData!.assumingMemoryBound(to: Float.self)
        var peak: Float = 0; for k in 0..<n { peak = max(peak, abs(p[k])) }
        stats.peaks[i] = max(stats.peaks[i], peak)
    }
}
log("ioproc: \(st) start: \(AudioDeviceStart(agg, procID))")
let say = Process(); say.executableURL = URL(fileURLWithPath: "/usr/bin/say"); say.arguments = ["-v", "Milena", "Проверка захвата системного звука транскрибатором"]
try? say.run()
Thread.sleep(forTimeInterval: 6)
AudioDeviceStop(agg, procID)
log("calls \(stats.calls) peaks \(stats.peaks)  (buffer0 = mic, buffer1 = system tap)")
AudioDeviceDestroyIOProcID(agg, procID!); AudioHardwareDestroyAggregateDevice(agg); AudioHardwareDestroyProcessTap(tapID)
log("probe end")
