import Foundation
import Testing
@testable import TranscriberCore

private func tab(_ url: String, _ title: String = "") -> BrowserTab {
    BrowserTab(title: title, url: url)
}

/// Корпоративный хост приходит из `privacy.workHosts`, а не из кода.
private let configuredHosts = BrowserTabs.defaultMeetingHosts + ["talk.example.com"]

@Test func meetingTabNeedsAMeetingHostAndARoomInThePath() {
    #expect(BrowserTabs.isMeeting(tab("https://talk.example.com/5170c4177c32422893005c8dd4ed834e"), hosts: configuredHosts))
    #expect(BrowserTabs.isMeeting(tab("https://ktalk.ru/room1")))
    #expect(BrowserTabs.isMeeting(tab("https://meet.google.com/abc-defg-hij")))
    #expect(BrowserTabs.isMeeting(tab("https://us02web.zoom.us/j/123")))
    #expect(BrowserTabs.isMeeting(tab("https://zoom.us/wc/123/join")))
    // Lobby pages: the host is right, but nobody is in a room.
    #expect(!BrowserTabs.isMeeting(tab("https://talk.example.com/"), hosts: configuredHosts))
    #expect(!BrowserTabs.isMeeting(tab("https://zoom.us/")))
    // Neighbouring hosts must not match on a substring.
    #expect(!BrowserTabs.isMeeting(tab("https://mail.google.com/x")))
    #expect(!BrowserTabs.isMeeting(tab("https://notzoom.us/j/123")))
    #expect(!BrowserTabs.isMeeting(tab("не ссылка")))
}

@Test func configuredHostCountsAsMeetingOnlyWhenPassed() {
    #expect(BrowserTabs.isMeeting(tab("https://talk.example.com/room1")) == false)
    #expect(BrowserTabs.isMeeting(tab("https://talk.example.com/room1"), hosts: configuredHosts) == true)
}

@Test func pickMeetingTakesTheFirstCallTab() {
    let tabs = [tab("https://mail.google.com/x", "Почта"),
                tab("https://talk.example.com/", "Толк"),
                tab("https://talk.example.com/room1", "Еженедельный синк — Толк"),
                tab("https://meet.google.com/abc-defg-hij", "Meet – Standup")]
    #expect(BrowserTabs.pickMeeting(tabs, hosts: configuredHosts) == tabs[2])
    #expect(BrowserTabs.pickMeeting([tab("https://talk.example.com/", "Толк")], hosts: configuredHosts) == nil)
    #expect(BrowserTabs.pickMeeting([]) == nil)
}

private let workHosts = ["talk.example.com", "ktalk.ru"]

@Test func workMeetingIsACallOnAWorkHost() {
    #expect(BrowserTabs.isWorkMeeting(tab("https://talk.example.com/room1"), hosts: workHosts))
    #expect(BrowserTabs.isWorkMeeting(tab("https://ktalk.ru/j/abc"), hosts: workHosts))
    #expect(BrowserTabs.isWorkMeeting(tab("https://my.ktalk.ru/room1"), hosts: workHosts))   // поддомен
    // Лобби — хост тот, но звонка ещё нет.
    #expect(!BrowserTabs.isWorkMeeting(tab("https://talk.example.com/"), hosts: workHosts))
    // Звонок, но не в Толке: такой не отменяет приватного окна.
    #expect(!BrowserTabs.isWorkMeeting(tab("https://us02web.zoom.us/j/123"), hosts: workHosts))
    #expect(!BrowserTabs.isWorkMeeting(tab("https://meet.google.com/abc-defg-hij"), hosts: workHosts))
    // Соседний хост не должен цепляться подстрокой.
    #expect(!BrowserTabs.isWorkMeeting(tab("https://notktalk.ru/room1"), hosts: workHosts))
    #expect(!BrowserTabs.isWorkMeeting(tab("не ссылка"), hosts: workHosts))
    #expect(!BrowserTabs.isWorkMeeting(tab("https://talk.example.com/room1"), hosts: []))
}

@Test func hasWorkMeetingLooksAtEveryTab() {
    let tabs = [tab("https://mail.google.com/x", "Почта"),
                tab("https://talk.example.com/", "Толк"),
                tab("https://talk.example.com/room1", "Еженедельный синк — Толк")]
    #expect(BrowserTabs.hasWorkMeeting(in: tabs, hosts: workHosts))
    #expect(!BrowserTabs.hasWorkMeeting(in: Array(tabs.prefix(2)), hosts: workHosts))
    #expect(!BrowserTabs.hasWorkMeeting(in: [], hosts: workHosts))
    // Браузер, который ничего не ответил, — не повод считать звонок рабочим.
    #expect(!BrowserTabs.hasWorkMeeting(forBrowser: "com.example.nope", hosts: workHosts))
}

@Test func cleanTitleStripsServiceNamesOnly() {
    #expect(BrowserTabs.cleanTitle("Еженедельный синк — Толк") == "Еженедельный синк")
    #expect(BrowserTabs.cleanTitle("Standup - Google Meet") == "Standup")
    #expect(BrowserTabs.cleanTitle("Meet – Standup") == "Standup")
    #expect(BrowserTabs.cleanTitle("Google Meet: Разбор инцидента") == "Разбор инцидента")
    #expect(BrowserTabs.cleanTitle("  Синк   про   роадмап  ") == "Синк про роадмап")
    // Nothing but the service name is not a meeting title.
    #expect(BrowserTabs.cleanTitle("Толк") == "")
    #expect(BrowserTabs.cleanTitle(" Zoom ") == "")
    #expect(BrowserTabs.cleanTitle("") == "")
    // A meeting that genuinely mentions the service keeps its name.
    #expect(BrowserTabs.cleanTitle("Толк про роадмап") == "Толк про роадмап")
}

@Test func tabsAreParsedFromTabSeparatedLines() {
    let raw = "Еженедельный синк — Толк\thttps://talk.example.com/room1\nТолк\thttps://talk.example.com/\nсломанная строка\n"
    #expect(BrowserTabs.parse(raw) == [BrowserTab(title: "Еженедельный синк — Толк", url: "https://talk.example.com/room1"),
                                       BrowserTab(title: "Толк", url: "https://talk.example.com/")])
    #expect(BrowserTabs.parse("") == [])
}

@Test func unknownBrowserYieldsNoTabs() {
    #expect(BrowserTabs.tabs(ofBrowser: "com.example.nope") == [])
    #expect(BrowserTabs.meetingInfo(forBrowser: "com.example.nope", hosts: configuredHosts) == nil)
}

@Test func isPlaceholderMatchesKnownLoadingTitles() {
    #expect(BrowserTabs.isPlaceholder("Подключение к встрече"))
    #expect(BrowserTabs.isPlaceholder("подключение к встрече"))   // case-insensitive
    #expect(BrowserTabs.isPlaceholder("Подключение"))
    #expect(BrowserTabs.isPlaceholder("Connecting"))
    #expect(BrowserTabs.isPlaceholder("CONNECTING"))
    #expect(BrowserTabs.isPlaceholder("Толк"))
    #expect(BrowserTabs.isPlaceholder("Загрузка"))
    #expect(BrowserTabs.isPlaceholder("Loading"))
    #expect(BrowserTabs.isPlaceholder(""))
    // Any «Подключение к …» reads as still-connecting, not just the exact phrase.
    #expect(BrowserTabs.isPlaceholder("Подключение к комнате"))
    // A real meeting title is not a placeholder, even one that mentions the service.
    #expect(!BrowserTabs.isPlaceholder("Еженедельный синк"))
    #expect(!BrowserTabs.isPlaceholder("Толк про роадмап"))
}

@Test func meetingTabStateWaitsOutAPlaceholderTitle() {
    // Pre-join screen: URL is already a room, title has not caught up yet.
    let loading = [tab("https://talk.example.com/room1", "Подключение к встрече")]
    #expect(BrowserTabs.meetingTabState(in: loading, hosts: configuredHosts) == .placeholder)

    let ready = [tab("https://talk.example.com/room1", "Еженедельный синк — Толк")]
    #expect(BrowserTabs.meetingTabState(in: ready, hosts: configuredHosts) ==
             .ready(MeetingInfo(title: "Еженедельный синк", participants: [])))

    let noCallTab = [tab("https://mail.google.com/x", "Почта")]
    #expect(BrowserTabs.meetingTabState(in: noCallTab, hosts: configuredHosts) == .none)
}
