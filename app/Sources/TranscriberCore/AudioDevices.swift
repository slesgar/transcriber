import CoreAudio
import Foundation

/// The default input/output devices by name, and a listener for when macOS switches them.
///
/// Why this exists: a session that started on a Bluetooth headset runs at 24 kHz, and when the
/// headset drops off the tap keeps running while the mic track stalls — the log then shows only
/// «input device disappeared». Naming both devices at capture start, and logging every switch,
/// turns that into a readable story. Nothing here changes routing: we only look.
public enum AudioDevices {
    public typealias Device = (id: AudioObjectID, name: String, uid: String)

    public static func defaultInput() -> Device? {
        device(kAudioHardwarePropertyDefaultInputDevice)
    }

    public static func defaultOutput() -> Device? {
        device(kAudioHardwarePropertyDefaultOutputDevice)
    }

    private static func device(_ selector: AudioObjectPropertySelector) -> Device? {
        let system = AudioObjectID(kAudioObjectSystemObject)
        let id: AudioObjectID = CoreAudioProps.get(system, selector, default: AudioObjectID(0))
        guard id != 0 else { return nil }
        return (id: id,
                name: CoreAudioProps.getString(id, kAudioObjectPropertyName),
                uid: CoreAudioProps.getString(id, kAudioDevicePropertyDeviceUID))
    }

    /// Name of a device, or a placeholder when Core Audio has none — the callers all log it.
    public static func name(of device: Device?) -> String {
        guard let device, !device.name.isEmpty else { return device == nil ? "нет" : "без имени" }
        return device.name
    }

    /// Calls `handler("input"/"output", name)` on the main queue whenever macOS switches the
    /// default device. Keep the returned token alive: releasing it removes the listeners.
    public static func observeDefaultDeviceChanges(_ handler: @escaping (_ kind: String, _ name: String) -> Void) -> Any {
        DeviceObserver(handler: handler)
    }
}

/// Owns the two property listeners; `deinit` unregisters them, so the caller only has to hold on
/// to the object for as long as the notifications are wanted.
private final class DeviceObserver {
    private let system = AudioObjectID(kAudioObjectSystemObject)
    private var registered: [(AudioObjectPropertyAddress, AudioObjectPropertyListenerBlock)] = []

    init(handler: @escaping (String, String) -> Void) {
        add(kAudioHardwarePropertyDefaultInputDevice, kind: "input") {
            handler("input", AudioDevices.name(of: AudioDevices.defaultInput()))
        }
        add(kAudioHardwarePropertyDefaultOutputDevice, kind: "output") {
            handler("output", AudioDevices.name(of: AudioDevices.defaultOutput()))
        }
    }

    private func add(_ selector: AudioObjectPropertySelector, kind: String, notify: @escaping () -> Void) {
        var addr = CoreAudioProps.address(selector)
        // Reading the new device is a Core Audio round trip; the handler drives UI and logging, so
        // both belong on main — the listener queue decides where the block runs.
        let block: AudioObjectPropertyListenerBlock = { _, _ in notify() }
        let status = AudioObjectAddPropertyListenerBlock(system, &addr, DispatchQueue.main, block)
        guard status == noErr else {
            Log.shared.warn("devices: listener for default \(kind) failed, OSStatus \(status)")
            return
        }
        registered.append((addr, block))
    }

    deinit {
        for (address, block) in registered {
            var addr = address
            AudioObjectRemovePropertyListenerBlock(system, &addr, DispatchQueue.main, block)
        }
    }
}
