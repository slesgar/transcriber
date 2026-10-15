import Foundation

/// Why a recording stopped — one word in the log for every path out of `.recording`. Until
/// 14.10.2026 a stop logged only the sample count, and the two cut-offs of 12–13.10 (Еженедельный синк,
/// Ревью документа) could not be told apart from a crash: a hand on the button and «meeting ended»
/// looked the same.
public enum StopReason: String {
    case manual = "вручную"                   // menu or the floating button
    case silencePrompt = "карточка тишины"    // «Остановить» on the silence card
    case silenceAuto = "тишина"               // no voice for `silenceStopAfter`, nobody answered
    case meetingEnded = "встреча закончилась" // the detector saw the mic released
    case quit = "выход из приложения"
    case sigterm = "SIGTERM"
}

/// What happens when the «Записать?» card goes unanswered. A work call starts recording on its
/// own after 30 s: several meetings were lost to a card nobody clicked. «Work» is simply «not
/// private» by the rules of the moment (Толк desktop, a browser call outside the private
/// windows, a work host inside them);
/// the narrower `workContext` flag only says how the verdict was reached. A private call still
/// needs the click: nothing may enter the private folder by default, and the verdict is taken
/// again when the timer fires, so a menu override made during those seconds counts.
public enum UnansweredPrompt: Equatable {
    case start
    case decline

    public static func action(isPrivate: Bool) -> UnansweredPrompt {
        isPrivate ? .decline : .start
    }
}

public enum PromptTimeouts {
    /// Seconds the meeting card waits before `UnansweredPrompt.action` decides.
    public static let meeting: TimeInterval = 30
    /// The private card keeps the old patience: there is no auto-start behind it.
    public static let privateMeeting: TimeInterval = 90
}
