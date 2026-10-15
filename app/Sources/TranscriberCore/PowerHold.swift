import Foundation
import IOKit.pwr_mgt

/// Keeps the Mac awake while recording: a power assertion against idle sleep, plus (when the
/// sudoers rule exists) `pmset disablesleep` so a closed lid does not sleep either.
public final class PowerHold {
    private var assertion: IOPMAssertionID = 0
    private var held = false      // the IOPM assertion is live
    private var pmsetOn = false   // `pmset disablesleep 1` went through and must be undone
    private let usePmset: Bool
    private static var didResetAtLaunch = false

    public init(usePmset: Bool = true) {
        self.usePmset = usePmset
    }

    public func acquire() {
        guard !held, !pmsetOn else { return }
        let status = IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleSystemSleep as CFString,
                                                 IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                                 "Транскрибатор пишет встречу" as CFString, &assertion)
        if status == kIOReturnSuccess {
            held = true
        } else {
            assertion = 0
            Log.shared.warn("power assertion failed: \(status)")
        }
        if usePmset { pmsetOn = Self.pmset(disableSleep: true) }
    }

    public func release() {
        if held {
            IOPMAssertionRelease(assertion)
            assertion = 0
            held = false
        }
        if pmsetOn {
            _ = Self.pmset(disableSleep: false)
            pmsetOn = false
        }
    }

    /// Undoes a `pmset disablesleep 1` left behind by a crash mid-session. Safe to call always;
    /// runs at most once per process.
    public static func resetAtLaunch() {
        guard !didResetAtLaunch else { return }
        didResetAtLaunch = true
        _ = pmset(disableSleep: false)
    }

    /// Returns true when the setting actually took (the sudoers rule exists).
    @discardableResult
    public static func pmset(disableSleep: Bool) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/sudo")
        p.arguments = ["-n", "/usr/bin/pmset", "-a", "disablesleep", disableSleep ? "1" : "0"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            p.waitUntilExit()
            if p.terminationStatus != 0 {
                Log.shared.warn("pmset disablesleep \(disableSleep ? 1 : 0) skipped (exit \(p.terminationStatus)); add the sudoers rule to enable it")
                return false
            }
            Log.shared.info("pmset disablesleep \(disableSleep ? 1 : 0)")
            return true
        } catch {
            Log.shared.warn("pmset launch failed: \(error)")
            return false
        }
    }
}
