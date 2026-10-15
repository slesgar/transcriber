import CoreAudio
import Foundation

public struct AudioProcess: Equatable {
    public let objectID: AudioObjectID
    public let pid: pid_t
    public let bundleID: String
    public let isRunningInput: Bool

    public init(objectID: AudioObjectID, pid: pid_t, bundleID: String, isRunningInput: Bool) {
        self.objectID = objectID
        self.pid = pid
        self.bundleID = bundleID
        self.isRunningInput = isRunningInput
    }
}

enum CoreAudioProps {
    static func address(_ selector: AudioObjectPropertySelector,
                        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func get<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                       scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, default value: T) -> T {
        var addr = address(selector, scope: scope)
        var size = UInt32(MemoryLayout<T>.size)
        var out = value
        let status = AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &out)
        return status == noErr ? out : value
    }

    static func getString(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String {
        var addr = address(selector)
        var size = UInt32(MemoryLayout<CFString?>.size)
        var value: Unmanaged<CFString>? = nil
        let status = AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value)
        guard status == noErr, let v = value?.takeRetainedValue() else { return "" }
        return v as String
    }

    static func getArray<T: FixedWidthInteger>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, of: T.Type) -> [T] {
        var addr = address(selector)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var items = [T](repeating: T.zero, count: Int(size) / MemoryLayout<T>.size)
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &items) == noErr else { return [] }
        return items
    }
}

public enum AudioProcesses {
    /// Our own bundle id — excluded from mic-holder diagnostics, matched for the teardown check.
    public static let ownBundleID = "ru.slesarev.transcriber"

    /// Every process Core Audio knows about, with whether it currently records from an input device.
    public static func list() -> [AudioProcess] {
        let system = AudioObjectID(kAudioObjectSystemObject)
        return CoreAudioProps.getArray(system, kAudioHardwarePropertyProcessObjectList, of: AudioObjectID.self).map { obj in
            AudioProcess(
                objectID: obj,
                pid: CoreAudioProps.get(obj, kAudioProcessPropertyPID, default: pid_t(0)),
                bundleID: CoreAudioProps.getString(obj, kAudioProcessPropertyBundleID),
                isRunningInput: CoreAudioProps.get(obj, kAudioProcessPropertyIsRunningInput, default: UInt32(0)) != 0
            )
        }
    }

    /// Whether Core Audio still reports *us* as recording from an input device. Purely
    /// diagnostic: after a session stops this must be false, and a lingering true is the
    /// signature of a tap or aggregate device that outlived its session.
    public static func selfIsRunningInput() -> Bool {
        let me = getpid()
        return list().contains { ($0.pid == me || $0.bundleID.hasPrefix(ownBundleID)) && $0.isRunningInput }
    }

    /// Core Audio object for our own process, used to exclude ourselves from the tap.
    public static func ownProcessObject() -> AudioObjectID? {
        var addr = CoreAudioProps.address(kAudioHardwarePropertyTranslatePIDToProcessObject)
        var pid = getpid()
        var out = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                                                UInt32(MemoryLayout<pid_t>.size), &pid, &size, &out)
        return status == noErr && out != kAudioObjectUnknown ? out : nil
    }
}
