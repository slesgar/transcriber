import json
import math
from pathlib import Path

from transcriber.talk import (TalkFile, active_timeline, is_bot, is_room, load_talk, span_share,
                              speaker_for_span, talk_participants)

ПЁТР = "Пётр Кузьмин"
КОМНАТА = "Переговорная 5"
БОТ = "KTalk Secretary (саммаризатор встреч)"


def sample(t: float, *active: str, names: list[str] | None = None, mic: str | None = None) -> dict:
    """Снимок плиток: перечисленные в ``active`` подсвечены, остальные — нет."""
    names = names or list(active)
    return {"t": t, "participants": [
        {"name": n, "mic": n == mic, "active": n in active} for n in names]}


def write_talk(d: Path, samples: list[dict]) -> Path:
    p = d / "talk.jsonl"
    p.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n", encoding="utf-8")
    return p


# ---- чтение файла ------------------------------------------------------------


def test_missing_file_is_an_empty_timeline(tmp_path):
    assert load_talk(tmp_path / "нет.jsonl") == []
    assert active_timeline(load_talk(tmp_path / "нет.jsonl")) == []
    assert speaker_for_span([], 0.0, 5.0) is None


def test_truncated_last_line_and_junk_keys_do_not_break_the_read(tmp_path):
    p = tmp_path / "talk.jsonl"
    p.write_text("\n".join([
        json.dumps(sample(0.0, ПЁТР), ensure_ascii=False),
        json.dumps({"t": 5.0, "participants": [{"name": ПЁТР, "active": True, "странное": 1}], "ещё": "поле"},
                   ensure_ascii=False),
        "",
        '{"t": 9.5, "participants": [{"name": "Обо',          # файл пишется на ходу — хвост оборван
    ]), encoding="utf-8")
    samples = load_talk(p)
    assert [s.t for s in samples] == [0.0, 5.0]
    assert samples[1].participants[0].name == ПЁТР and samples[1].participants[0].mic is False


def test_nameless_and_malformed_samples_are_skipped(tmp_path):
    p = tmp_path / "talk.jsonl"
    p.write_text("\n".join([
        json.dumps({"participants": []}),                      # без t — выборка бессмысленна
        json.dumps({"t": "потом", "participants": []}),
        json.dumps([1, 2, 3]),
        json.dumps({"t": 1.0, "participants": [{"name": "  "}, {"нет": "имени"}, "строка"]}),
        json.dumps({"t": 2.0}),                                # без плиток — все замолчали
    ]), encoding="utf-8")
    samples = load_talk(p)
    assert [(s.t, len(s.participants)) for s in samples] == [(1.0, 0), (2.0, 0)]


# ---- переговорки и боты ------------------------------------------------------


def test_rooms_and_bots_are_told_by_the_default_patterns():
    assert is_room(КОМНАТА) is True
    assert is_room("Переговорка 4") is True and is_room("Meeting Room") is True
    assert is_room(ПЁТР) is False and is_room("Иван Сидоров") is False
    assert is_bot(БОТ) is True and is_bot("Telegram bot") is True
    assert is_bot(ПЁТР) is False
    assert is_room("Переговорная", patterns=["^нечто"]) is False    # шаблоны берутся из конфига


def test_a_broken_pattern_does_not_break_the_check():
    assert is_room(КОМНАТА, patterns=["(", "переговорная"]) is True


# ---- лента активности --------------------------------------------------------


def test_timeline_holds_a_state_until_the_next_sample():
    samples = load_talk_from([sample(0.0, ПЁТР, names=[ПЁТР, "Иван Сидоров"]),
                              sample(4.0, "Иван Сидоров", names=[ПЁТР, "Иван Сидоров"]),
                              sample(9.0, names=[ПЁТР, "Иван Сидоров"])])
    tl = active_timeline(samples)
    assert tl == [(0.0, 4.0, ПЁТР), (4.0, 9.0, "Иван Сидоров")]   # с 9.0 — пауза, никто не активен


def test_timeline_merges_neighbours_and_runs_to_infinity():
    samples = load_talk_from([sample(0.0, ПЁТР), sample(3.0, ПЁТР), sample(6.0, ПЁТР)])
    tl = active_timeline(samples)
    assert len(tl) == 1 and tl[0][0] == 0.0 and tl[0][2] == ПЁТР
    assert math.isinf(tl[0][1])                                 # последняя выборка держится до конца


def test_several_active_tiles_take_the_first():
    samples = load_talk_from([sample(0.0, ПЁТР, "Иван Сидоров", names=[ПЁТР, "Иван Сидоров"])])
    assert active_timeline(samples)[0][2] == ПЁТР


def load_talk_from(samples: list[dict]):
    from transcriber.talk import TalkSample, Tile

    return [TalkSample(float(s["t"]), [Tile(p["name"], bool(p.get("mic")), bool(p.get("active")))
                                       for p in s["participants"]]) for s in samples]


# ---- имя на отрезок ----------------------------------------------------------


TIMELINE = [(0.0, 10.0, ПЁТР), (10.0, 20.0, "Иван Сидоров"), (20.0, 30.0, КОМНАТА), (30.0, 40.0, БОТ)]


def test_span_fully_inside_one_name():
    assert speaker_for_span(TIMELINE, 2.0, 8.0) == ПЁТР
    assert span_share(TIMELINE, 2.0, 8.0, ПЁТР) == 1.0


def test_span_below_the_share_gets_no_name():
    assert speaker_for_span(TIMELINE, 8.0, 12.0) is None        # ровно пополам — никто не набрал 0.6
    assert speaker_for_span(TIMELINE, 9.0, 12.0) == "Иван Сидоров"
    assert speaker_for_span(TIMELINE, 7.0, 12.0, min_share=0.5) == ПЁТР   # 3 с из 5 — хватает


def test_rooms_and_bots_never_name_a_span():
    assert speaker_for_span(TIMELINE, 21.0, 29.0) is None       # переговорка — это не один говорящий
    assert speaker_for_span(TIMELINE, 31.0, 39.0) is None
    assert span_share(TIMELINE, 21.0, 29.0, КОМНАТА) == 1.0     # доля считается, имя не выдаётся


def test_span_outside_the_timeline_or_empty_gets_no_name():
    assert speaker_for_span(TIMELINE, 100.0, 110.0) is None
    assert speaker_for_span(TIMELINE, 5.0, 5.0) is None
    assert span_share(TIMELINE, 5.0, 5.0, ПЁТР) == 0.0


# ---- участники встречи -------------------------------------------------------


def test_participants_are_the_union_without_bots():
    samples = load_talk_from([
        sample(0.0, ПЁТР, names=[ПЁТР, БОТ]),
        sample(5.0, КОМНАТА, names=[ПЁТР, КОМНАТА, БОТ]),
        sample(9.0, names=[ПЁТР, КОМНАТА]),
    ])
    assert talk_participants(samples) == [ПЁТР, КОМНАТА]         # переговорка — живой участник, бот — нет


# ---- ленивое перечитывание ---------------------------------------------------


def test_talk_file_rereads_only_when_the_file_changed(tmp_path):
    f = TalkFile(tmp_path / "talk.jsonl")
    assert f.timeline == [] and f.refresh() is False            # файла может не быть вовсе

    write_talk(tmp_path, [sample(0.0, ПЁТР)])
    assert f.refresh() is True
    assert f.timeline[0][2] == ПЁТР
    assert f.refresh() is False                                 # ничего не изменилось — не перечитываем

    write_talk(tmp_path, [sample(0.0, ПЁТР), sample(8.0, "Иван Сидоров")])
    assert f.refresh() is True
    assert [span[2] for span in f.timeline] == [ПЁТР, "Иван Сидоров"]
    assert f.participants() == [ПЁТР, "Иван Сидоров"]


def test_shared_mic_names_never_become_speakers():
    from transcriber.talk import is_shared, speaker_for_span
    tl = [(0.0, 10.0, "Николай Фомин"), (10.0, 20.0, "Татьяна Белова")]
    assert is_shared("Николай Фомин", ["николай фомин"])
    assert not is_shared("Татьяна Белова", ["Николай Фомин"])
    assert speaker_for_span(tl, 0.0, 9.0, 0.6, shared_names=["Николай Фомин"]) is None
    assert speaker_for_span(tl, 10.0, 19.0, 0.6, shared_names=["Николай Фомин"]) == "Татьяна Белова"


def test_next_best_name_when_top_cover_is_a_shared_mic():
    from transcriber.talk import speaker_for_span
    tl = [(0.0, 4.0, "Николай Фомин"), (4.0, 10.0, "Татьяна Белова")]
    # 60 % отрезка за Татьяной, 40 % за общим микрофоном — имя получает Татьяна
    assert speaker_for_span(tl, 0.0, 10.0, 0.6, shared_names=["Николай Фомин"]) == "Татьяна Белова"
    assert speaker_for_span(tl, 0.0, 10.0, 0.6) == "Татьяна Белова"
    tl2 = [(0.0, 7.0, "Николай Фомин"), (7.0, 10.0, "Татьяна Белова")]
    assert speaker_for_span(tl2, 0.0, 10.0, 0.6, shared_names=["Николай Фомин"]) is None


def test_status_suffix_is_stripped_from_tile_name():
    from transcriber.talk import clean_name
    assert clean_name("Алексей Беляев Связь потеряна. Пытаемся восстановить…") == "Алексей Беляев"
    assert clean_name("  Анна  Петрова ") == "Анна Петрова"
    assert clean_name("Связь потеряна") == "Связь потеряна"
    assert clean_name("Иван Сидоров Передача видео в высоком качестве") == "Иван Сидоров"
    assert clean_name("Иван Сидоров Проверяем скорость вашего интернета, видео скоро появится") == "Иван Сидоров"
    assert clean_name("Иван Сидоров Демонстрация экрана") == "Иван Сидоров"
