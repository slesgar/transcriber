import Foundation
import CoreAudio
import AudioToolbox

// 1. List audio processes and whether they are running input (mic) — meeting detection.
func processObjects() -> [AudioObjectID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    return ids
}
func bundleID(_ obj: AudioObjectID) -> String {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyBundleID,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = UInt32(MemoryLayout<CFString?>.size)
    var value: Unmanaged<CFString>? = nil
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value)
    guard st == noErr, let v = value?.takeRetainedValue() else { return "" }
    return v as String
}
func isRunningInput(_ obj: AudioObjectID) -> Bool {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyIsRunningInput,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = UInt32(MemoryLayout<UInt32>.size)
    var value: UInt32 = 0
    AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value)
    return value != 0
}
for obj in processObjects() {
    let b = bundleID(obj)
    if !b.isEmpty { print(obj, b, isRunningInput(obj) ? "INPUT" : "") }
}

// 2. Create a process tap on all processes (except ourselves) + aggregate device: compile-check + creation.
let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
desc.name = "probe-tap"
desc.isPrivate = true
desc.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
let st = AudioHardwareCreateProcessTap(desc, &tapID)
print("tap status", st, "tapID", tapID)
if st == noErr {
    // read tap stream format
    var addr = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var fmt = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let s2 = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &fmt)
    print("tap format status", s2, "rate", fmt.mSampleRate, "ch", fmt.mChannelsPerFrame, "bits", fmt.mBitsPerChannel)
    AudioHardwareDestroyProcessTap(tapID)
}
