import json
from datetime import datetime
from pathlib import Path

from transcriber import bench_ref
from transcriber.bench_ref import (aggregate, compare_transcripts, find_reference, parse_local,
                                   parse_reference, run_bench_ref, same_person, surname)
from transcriber.config import Config
from transcriber.session import Session, State

from .conftest import make_session

ANNA, IVAN, OLGA = "Анна Петрова", "Иван Сидоров", "Ольга Смирнова"
SPEECH_A = "мы договорились перенести запуск на следующую неделю и собрать метрики по сегментам"
SPEECH_B = "давайте проверим что выгрузка по кассам сходится с витриной до конца дня"
SPEECH_C = "у нас осталось два открытых вопроса по бюджету и по срокам тестирования"

REF = f"""---
type: raw
source: talkbot
---

# Еженедельный синк

### Кратко

Команда обсудила планы.

## Полный транскрипт

<details>

[00:05] Петрова Анна: {SPEECH_A}.
[00:40] Сидоров Иван: {SPEECH_B}.
[01:10] Петрова Анна: {SPEECH_C}.
[01:40] Смирнова Ольга: это было только у бота и довольно длинная реплика о чём то важном для всех нас сегодня.

</details>
"""

LOCAL = f"""---
type: raw
---

# Еженедельный синк — 2026-09-15 14:30

Локальная запись. Статус: финализирован.

## Полный транскрипт

{ANNA}: {SPEECH_A}.

Me: {SPEECH_B}.

{ANNA}: {SPEECH_C}.

Speaker 3: а это галлюцинация которой у бота нет и она тоже довольно длинная чтобы попасть в список кусков.
"""


def test_parse_reference_and_local_lines():
    ref = parse_reference(REF)
    assert ref[0] == (5.0, "Петрова Анна", SPEECH_A + ".") and len(ref) == 4
    loc = parse_local(LOCAL)
    assert loc[0] == (0, ANNA, SPEECH_A + ".") and loc[1][1] == "Me" and len(loc) == 4


def test_same_person_tolerates_patronymics_order_and_yo():
    assert same_person("Анна Петрова", "Петрова Анна Сергеевна")
    assert same_person("Пётр Кузьмин", "Кузьмин Петр")
    assert same_person("Анна Ли", "Ли Анна")
    assert not same_person("Иван Петров", "Иван Сидоров")          # общее только имя
    assert not same_person("Анна Петрова", "Ольга Смирнова")
    assert surname("") == "" and surname("Анна Петрова") == "петрова"


def test_compare_measures_coverage_speakers_names_and_lost_passages():
    res = compare_transcripts(parse_reference(REF), parse_local(LOCAL))
    assert res["same_meeting"] and res["common"] >= 30
    assert 0.5 < res["coverage_ref"] < 1.0 and 0.5 < res["coverage_local"] < 1.0
    assert res["speaker_consistency"] == 1.0
    by = {m["local"]: m for m in res["speakers"]}
    assert by[ANNA]["reference"] == "Петрова Анна" and by[ANNA]["same_person"] is True
    assert by["Me"]["reference"] == "Сидоров Иван" and by["Me"]["same_person"] is None
    assert res["names_agree"] == 1.0
    assert len(res["only_reference"]) == 1 and "бота" in res["only_reference"][0]["text"]
    assert len(res["only_local"]) == 1 and "галлюцинация" in res["only_local"][0]["text"]


def test_compare_flags_a_different_meeting():
    other = "[00:01] Кузьмин Пётр: " + " ".join(f"слово{i}" for i in range(60)) + "."
    res = compare_transcripts(parse_reference(other), parse_local(LOCAL))
    assert res["same_meeting"] is False and res["common"] == 0


def test_find_reference_matches_date_start_and_title(tmp_path):
    d = tmp_path / "ref"
    d.mkdir()
    (d / "Bot-2026-09-15-1430-Еженедельный-синк.md").write_text(REF)
    (d / "Bot-2026-09-15-1431-Совсем-другая-тема.md").write_text(REF)
    (d / "Bot-2026-09-15-1700-Еженедельный-синк.md").write_text(REF)
    (d / "Bot-2026-09-16-1430-Еженедельный-синк.md").write_text(REF)
    start = datetime.fromisoformat("2026-09-15T14:32:00+03:00")
    assert find_reference(d, start, "Еженедельный синк").name == "Bot-2026-09-15-1430-Еженедельный-синк.md"
    assert find_reference(d, start, "Ревью документа") is None
    assert find_reference(tmp_path / "nope", start, "Еженедельный синк") is None


def _session_with_raw(cfg: Config, vault: Path, session_id: str = "2026-09-15-1430", raw_text: str = LOCAL) -> Path:
    d = make_session(cfg.sessions_dir, session_id, title="Еженедельный синк")
    raw = vault / "Memory/50 · Raw/Local" / f"Local-{session_id}-Еженедельный-синк.md"
    raw.write_text(raw_text)
    s = Session.load(d)
    s.state = State(status="finalized", raw_path=str(raw))
    s.save_state()
    return d


def test_run_writes_metrics_report_and_table(vault, tmp_path, capsys):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    ref_dir = vault / cfg.reference_subdir
    ref_dir.mkdir(parents=True)
    (ref_dir / "Bot-2026-09-15-1430-Еженедельный-синк.md").write_text(REF)
    _session_with_raw(cfg, vault)
    make_session(cfg.sessions_dir, "2026-09-16-1000", title="Без бота")        # нет заметки — вне отчёта
    run = run_bench_ref(cfg, "current", note="первый прогон")
    m = json.loads((run / "metrics.json").read_text())
    assert m["totals"]["sessions"] == 1 and m["totals"]["unmatched"] == 0 and m["totals"]["names_agree"] == 1.0
    assert "2026-09-15-1430" in (run / "report.md").read_text()
    assert "мы пропустили" in (run / "report.md").read_text()
    table = bench_ref.format_table(bench_ref.collect_rows(run.parent))
    assert "00-current" in table and "покр. бота" in table


def test_aggregate_skips_pairs_that_are_not_the_same_meeting():
    rows = [{"same_meeting": True, "words_ref": 100, "words_local": 80, "common": 60, "speaker_consistency": 0.9, "names_agree": 1.0},
            {"same_meeting": False, "words_ref": 500, "words_local": 500, "common": 2, "speaker_consistency": 0.5, "names_agree": 0.0}]
    t = aggregate(rows)
    assert t["sessions"] == 1 and t["unmatched"] == 1 and t["coverage_ref"] == 0.6 and t["names_agree"] == 1.0


def test_cli_bench_ref_prints_tables(vault, tmp_path, capsys, monkeypatch):
    from transcriber import cli
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    ref_dir = vault / cfg.reference_subdir
    ref_dir.mkdir(parents=True)
    (ref_dir / "Bot-2026-09-15-1430-Еженедельный-синк.md").write_text(REF)
    _session_with_raw(cfg, vault)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    cli.main(["bench-ref", "--label", "current"])
    out = capsys.readouterr().out
    assert "2026-09-15-1430" in out and "00-current" in out and "говорящие" in out


def test_room_labels_at_the_bot_are_not_name_disagreements():
    ref = parse_reference("[00:01] 529 Прорыв: " + SPEECH_A + ".\n[00:30] Переговорная 5: " + SPEECH_B + ".")
    loc = parse_local("## Полный транскрипт\n\n" + ANNA + ": " + SPEECH_A + ".\n\n" + IVAN + ": " + SPEECH_B + ".\n")
    res = compare_transcripts(ref, loc, ["переговор"])
    assert all(m["same_person"] is None for m in res["speakers"]) and res["names_agree"] is None
