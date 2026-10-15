import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml
from transcriber import bench
from transcriber.config import Config
from transcriber.session import Rec
from transcriber.voices import VoiceLibrary
from tests.conftest import FakeAsr, FakeEmbedder, fake_prob, silence, to_pcm_bytes, tone

from transcriber import daemon


def fake_models():
    return daemon.Models(asr=FakeAsr(), embed=FakeEmbedder(), prob_factory=lambda: fake_prob)


RAW = """---
type: raw
source: local
---

# Встреча — 2026-09-21 10:30

Локальная запись. Говорящих на канале собеседников: 2. Статус: финализирован 11:10.

## Полный транскрипт

Me: Привет всем, давайте начнём с метрик за неделю

Speaker 1: Привет, у нас всё готово

— 00:05 —

Мария Иванова: Термины, Участники.

Speaker 1: Мы отдадим SQL в среду, я думаю этого хватит на первый заход

Me: [не распознано 06:10–06:14]
"""


# ---- метрики из транскрипта ---------------------------------------------------


def test_transcript_metrics_reads_lines_words_and_speakers():
    m = bench.transcript_metrics(RAW)
    assert m["lines"] == 5
    assert m["words_mic"] == 8 + 2              # обе реплики Me; в «[не распознано 06:10–06:14]» два слова
    assert m["words_sys"] == 5 + 2 + 12         # таймкоды и числа словами не считаются
    assert m["words"] == m["words_mic"] + m["words_sys"] == 29
    assert m["speakers"] == 2                   # Speaker 1 и Мария Иванова; Me не собеседник
    assert m["lines_per_speaker"] == {"Me": 2, "Speaker 1": 2, "Мария Иванова": 1}
    assert m["identified"] == ["Мария Иванова"]


def test_transcript_metrics_counts_junk_lines_and_segment_lengths():
    m = bench.transcript_metrics(RAW)
    assert m["junk_lines"] == 1                 # «Термины, Участники.» — утечка промпта
    assert m["words_per_line_mean"] == pytest.approx(29 / 5, abs=0.01)
    assert m["words_per_line_p95"] == 12.0


def test_transcript_metrics_ignores_the_header_and_time_markers():
    m = bench.transcript_metrics(RAW)
    assert "Локальная" not in json.dumps(m, ensure_ascii=False)
    assert m["lines"] == 5                      # строка «— 00:05 —» репликой не считается


def test_transcript_metrics_of_an_empty_transcript_does_not_divide_by_zero():
    m = bench.transcript_metrics("## Полный транскрипт\n\n")
    assert m["lines"] == 0 and m["words"] == 0 and m["words_per_line_mean"] == 0.0


# ---- нумерация прогонов и метка -----------------------------------------------


def test_next_run_number_continues_after_the_existing_runs(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    assert bench.next_run_number(runs) == 0
    (runs / "00-live-before-fixes").mkdir()
    (runs / "01-prompt-and-filters").mkdir()
    (runs / "заметка.txt").write_text("не каталог прогона")
    assert bench.next_run_number(runs) == 2
    (runs / "07-дыра-в-нумерации").mkdir()
    assert bench.next_run_number(runs) == 8     # нумеруем от максимума, дыры не переиспользуем


def test_run_dir_name_pads_to_two_digits():
    assert bench.run_dir_name(0, "current-main") == "00-current-main"
    assert bench.run_dir_name(12, "current-main") == "12-current-main"


def test_check_label_refuses_a_path_separator_and_spaces():
    assert bench.check_label("prompt-and-filters") == "prompt-and-filters"
    for bad in ("../побег", "с пробелом", "", "a/b"):
        with pytest.raises(ValueError):
            bench.check_label(bad)


# ---- --set key=value ----------------------------------------------------------


def test_parse_overrides_casts_by_the_current_field_type():
    cfg = Config()
    got = bench.parse_overrides(["block_min_s=30", "voice_min_samples=5", "carry_context=false",
                                 "whisper_model=mlx-community/whisper-small", "language=en"], cfg)
    assert got == {"block_min_s": 30.0, "voice_min_samples": 5, "carry_context": False,
                   "whisper_model": "mlx-community/whisper-small", "language": "en"}
    assert isinstance(got["block_min_s"], float) and isinstance(got["voice_min_samples"], int)
    assert bench.parse_overrides(["keep_audio=true"], cfg) == {"keep_audio": True}
    assert bench.parse_overrides(["voices_dir=~/tmp/voices"], cfg)["voices_dir"] == Path("~/tmp/voices").expanduser()


def test_parse_overrides_rejects_unknown_keys_bad_values_and_lists():
    cfg = Config()
    with pytest.raises(ValueError, match="block_min"):       # подсказываем похожее имя
        bench.parse_overrides(["block_min=30"], cfg)
    with pytest.raises(ValueError, match="voice_min_samples"):
        bench.parse_overrides(["voice_min_samples=много"], cfg)
    with pytest.raises(ValueError, match="carry_context"):
        bench.parse_overrides(["carry_context=ага"], cfg)
    with pytest.raises(ValueError, match="hallucinations"):
        bench.parse_overrides(["hallucinations=раз,два"], cfg)
    with pytest.raises(ValueError, match="="):
        bench.parse_overrides(["block_min_s"], cfg)


# ---- события лога и заглушенные секунды ---------------------------------------


def test_count_log_events_groups_lines_by_rule():
    lines = [
        "10:00:00 INFO dropped (prompt leak) 1.0-2.0: Термины, Громов",
        "10:00:01 INFO dropped (repetitive) 3.0-4.0: да да да",
        "10:00:02 INFO dropped (filler) 5.0-6.0: ааа",
        "10:00:03 INFO dropped (foreign) 7.0-8.0: thank you for watching",
        "10:00:04 INFO echo dropped 9.0-10.0: мы отдадим SQL",
        "10:00:04 INFO echo residue (acoustic) dropped 9.5-10.5 kept=0.07: в вашем сердце",
        "10:00:05 INFO retry without prompt 11.0-12.0: Термины",
        "10:00:06 WARNING asr failed at 13.0-14.0: RuntimeError('boom')",
        "10:00:07 INFO voices: Speaker 1 → Мария Иванова (0.700, 12 реплик)",
    ]
    assert bench.count_log_events(lines) == {
        "prompt leak": 1, "repetitive": 1, "foreign": 1, "filler": 1,
        "echo dropped": 1, "echo residue (acoustic)": 1, "retry without prompt": 1, "asr failed": 1}
    assert bench.drops(bench.count_log_events(lines)) == 6     # ретрай и падение ASR — не выброшенная речь


def test_blanked_seconds_counts_only_guarded_records():
    recs = [Rec("sys", 0.0, 2.0, "живая речь", "Speaker 1"),
            Rec("sys", 4.0, 6.5, "", "Speaker 1"),            # снято фильтром
            Rec("mic", 8.0, 9.0, "", "Me"),
            Rec("mic", 10.0, 12.0, None, "Me")]               # ASR упал — это не «заглушено»
    assert bench.blanked_seconds(recs) == pytest.approx(3.5)


# ---- сравнение двух прогонов --------------------------------------------------


A = """## Полный транскрипт

Me: Привет, давайте начнём

— 00:05 —

Speaker 1: Мы в пятницу кинули описание метрик, там дофига всего написано, и мне кажется что имеет смысл пройтись детально на неделе

Speaker 1: Хорошо, договорились
"""

B = """## Полный транскрипт

Me: Привет, давайте начнём

— 00:05 —

Speaker 1: Хорошо, договорились
"""


def test_compare_reports_shares_and_the_passage_that_vanished():
    res = bench.compare(A, B)
    assert res["share_b"] == pytest.approx(1.0)        # всё, что есть в B, нашлось в A
    assert res["share_a"] == pytest.approx(5 / 25)     # из A пропал кусок на 20 слов
    assert res["only_b"] == []
    assert len(res["only_a"]) == 1
    gone = res["only_a"][0]
    assert gone["words"] == 20 and gone["at"] == "00:05"
    assert "пятницу" in gone["text"]


def test_compare_ignores_speaker_labels_punctuation_and_case():
    same = A.replace("Speaker 1:", "Мария Иванова:").replace("Привет, давайте", "ПРИВЕТ -- давайте")
    res = bench.compare(A, same)
    assert res["share_a"] == pytest.approx(1.0) and res["share_b"] == pytest.approx(1.0)
    assert res["only_a"] == [] and res["only_b"] == []


def test_compare_keeps_only_long_passages():
    short = A.replace("Speaker 1: Хорошо, договорились", "Speaker 1: Ладно, добазарились")
    res = bench.compare(A, short)
    assert res["only_a"] == [] and res["only_b"] == []   # разошлись два слова — это не регресс


def test_compare_survives_an_empty_transcript():
    res = bench.compare(A, "## Полный транскрипт\n\n")
    assert res["share_a"] == 0.0 and res["share_b"] == 0.0


# ---- таблица сравнения --------------------------------------------------------


def _seed_run(runs: Path, name: str, raw: str, metrics: dict | None = None) -> Path:
    d = runs / name
    d.mkdir(parents=True)
    (d / "raw.md").write_text(raw, encoding="utf-8")
    if metrics is not None:
        (d / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")
    return d


def test_collect_rows_falls_back_to_the_transcript_when_there_are_no_metrics(tmp_path):
    runs = tmp_path / "runs"
    _seed_run(runs, "00-live-before-fixes", RAW)
    _seed_run(runs, "01-current", RAW, {"lines": 5, "words": 29, "speakers": 2, "junk_lines": 1,
                                        "blanked_seconds": 12.5, "realtime_factor": 4.2,
                                        "log_events": {"echo dropped": 3}})
    rows = bench.collect_rows(runs)
    assert [r["label"] for r in rows] == ["00-live-before-fixes", "01-current"]
    assert rows[0]["words"] == 29 and rows[0]["junk"] == 1
    assert rows[0]["blanked"] is None and rows[0]["rtf"] is None and rows[0]["drops"] is None
    assert rows[1]["blanked"] == 12.5 and rows[1]["rtf"] == 4.2 and rows[1]["drops"] == 3
    table = bench.format_table(rows)
    assert "00-live-before-fixes" in table and "01-current" in table and "—" in table


# ---- сквозной прогон ----------------------------------------------------------


def _synthetic_bench(bench_dir: Path, bench_id: str = "synthetic") -> Path:
    d = bench_dir / bench_id
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "id": bench_id, "started_at": "2026-09-21T10:30:00+03:00", "app": "manual",
        "title": "Эталонная встреча", "participants": ["Иван Сидоров"]}, ensure_ascii=False))
    (d / "mic.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 440), silence(7.0)])))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([silence(2.0), tone(1.5, 880), silence(4.5)])))
    return d


def _vault_files(vault: Path) -> list[Path]:
    return [p for p in vault.rglob("*") if p.is_file()]


def test_bench_writes_a_run_and_touches_nothing_outside_it(vault, tmp_path):
    sessions = tmp_path / "sessions"
    cfg = Config(vault_root=vault, sessions_dir=sessions, bench_dir=tmp_path / "bench", min_embed_s=1.0)
    _synthetic_bench(cfg.bench_dir)

    run = bench.run_bench(cfg, "synthetic", "current-main", overrides={"block_min_s": 1.0},
                          note="первый прогон", models=fake_models())

    assert run == cfg.bench_dir / "synthetic/runs/00-current-main"
    assert sorted(p.name for p in run.iterdir()) == [
        "commit.txt", "config.yaml", "metrics.json", "note.txt", "raw.md", "worker.log"]
    raw = (run / "raw.md").read_text(encoding="utf-8")
    assert "Me: tone440" in raw and "Speaker 1: tone880" in raw and "финализирован" in raw
    assert (run / "note.txt").read_text(encoding="utf-8") == "первый прогон"
    assert (run / "commit.txt").read_text(encoding="utf-8").strip()

    saved = yaml.safe_load((run / "config.yaml").read_text(encoding="utf-8"))
    assert saved["block_min_s"] == 1.0                      # --set доехал до прогона
    assert isinstance(saved["vault_root"], str) and isinstance(saved["voices_dir"], str)

    m = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    assert m["label"] == "current-main" and m["words"] > 0 and m["lines"] > 0
    assert m["audio_minutes"] == pytest.approx(8 / 60, abs=0.01)
    assert m["wall_seconds"] > 0 and m["realtime_factor"] > 0
    assert m["speakers"] == 1 and m["clusters"] == 1
    assert set(m["log_events"]) >= {"prompt leak", "echo dropped", "retry without prompt"}
    assert m["blanked_seconds"] >= 0

    assert _vault_files(vault) == []                        # рабочий вольт не тронут
    assert not sessions.exists() or list(sessions.iterdir()) == []
    assert (cfg.bench_dir / "synthetic/mic.pcm").exists()   # эталонное аудио на месте


def test_bench_numbers_the_next_run_and_prints_the_whole_table(vault, tmp_path, capsys):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    d = _synthetic_bench(cfg.bench_dir)
    _seed_run(d / "runs", "00-live-before-fixes", RAW)

    run = bench.run_bench(cfg, "synthetic", "current-main", models=fake_models())
    assert run.name == "01-current-main"
    print(bench.format_table(bench.collect_rows(d / "runs")))
    out = capsys.readouterr().out
    assert "00-live-before-fixes" in out and "01-current-main" in out


def test_bench_refuses_an_unknown_bench_id(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    with pytest.raises(ValueError, match="нет"):
        bench.run_bench(cfg, "такой-записи-нет", "current-main", models=fake_models())


def test_bench_ignores_an_attempt_to_redirect_the_run_into_the_real_vault(vault, tmp_path):
    """`--set vault_root=…` не должен вывести прогон из песочницы."""
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _synthetic_bench(cfg.bench_dir)
    run = bench.run_bench(cfg, "synthetic", "побег", overrides={"vault_root": vault},
                          models=fake_models())
    assert (run / "raw.md").exists() and _vault_files(vault) == []


def test_bench_carries_the_talk_file_into_the_sandbox(vault, tmp_path):
    """У эталона может быть talk.jsonl — прогон должен видеть те же имена, что живая запись."""
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    d = _synthetic_bench(cfg.bench_dir)
    (d / "talk.jsonl").write_text(json.dumps(
        {"t": 0.0, "participants": [{"name": "Пётр Кузьмин", "mic": False, "active": True}]},
        ensure_ascii=False) + "\n", encoding="utf-8")
    run = bench.run_bench(cfg, "synthetic", "с-толком", overrides={"block_min_s": 1.0},
                          models=fake_models())
    raw = (run / "raw.md").read_text(encoding="utf-8")
    assert "Участники по данным Толка: Пётр Кузьмин." in raw
    assert (cfg.bench_dir / "synthetic/talk.jsonl").exists()      # эталон на месте
    assert yaml.safe_load((run / "config.yaml").read_text(encoding="utf-8"))["talk_autolearn"] is False
    assert VoiceLibrary(cfg.voices_dir).names() == []             # эталон не пополняет библиотеку


def test_bench_keeps_a_private_reference_recording_inside_the_sandbox(vault, tmp_path, private_dir):
    """Эталон с `private: true` не должен писать в рабочий каталог приватных записей."""
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench",
                 private_dir=private_dir)
    d = _synthetic_bench(cfg.bench_dir)
    meta = json.loads((d / "meta.json").read_text())
    meta["private"] = True
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    run = bench.run_bench(cfg, "synthetic", "приватный", models=fake_models())
    assert (run / "raw.md").exists() and _vault_files(vault) == []
    assert not private_dir.exists()


# ---- команды CLI --------------------------------------------------------------


def test_bench_command_runs_and_prints_the_table(vault, tmp_path, capsys, monkeypatch):
    from transcriber import cli

    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _synthetic_bench(cfg.bench_dir)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(bench, "load_models", lambda _cfg: fake_models())
    cli.main(["bench", "synthetic", "--label", "current-main", "--set", "block_min_s=1",
              "--note", "базовый прогон"])
    out = capsys.readouterr().out
    assert "00-current-main" in out and "слов" in out
    assert json.loads((cfg.bench_dir / "synthetic/runs/00-current-main/metrics.json").read_text())["words"] > 0


def test_bench_command_reports_a_bad_set_as_exit_2(vault, tmp_path, capsys, monkeypatch):
    from transcriber import cli

    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _synthetic_bench(cfg.bench_dir)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    with pytest.raises(SystemExit) as e:
        cli.main(["bench", "synthetic", "--label", "current-main", "--set", "нет_такого_ключа=1"])
    assert e.value.code == 2 and "нет_такого_ключа" in capsys.readouterr().err


def test_bench_diff_command_prints_shares_and_lost_passages(vault, tmp_path, capsys, monkeypatch):
    from transcriber import cli

    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    d = _synthetic_bench(cfg.bench_dir)
    _seed_run(d / "runs", "00-раньше", A)
    _seed_run(d / "runs", "01-теперь", B)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    cli.main(["bench-diff", "synthetic", "00-раньше", "01-теперь"])
    out = capsys.readouterr().out
    assert "00-раньше" in out and "01-теперь" in out
    assert "пятницу" in out and "00:05" in out

    cli.main(["bench-diff", "synthetic", "00", "01"])       # номера тоже принимаются
    assert "пятницу" in capsys.readouterr().out


def test_bench_diff_command_reports_an_unknown_run_as_exit_2(vault, tmp_path, capsys, monkeypatch):
    from transcriber import cli

    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    d = _synthetic_bench(cfg.bench_dir)
    _seed_run(d / "runs", "00-раньше", A)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    with pytest.raises(SystemExit) as e:
        cli.main(["bench-diff", "synthetic", "00-раньше", "09-небылого"])
    assert e.value.code == 2 and "09-небылого" in capsys.readouterr().err


def test_bench_dir_is_configurable(tmp_path):
    cfg = replace(Config(), bench_dir=tmp_path / "стенд")
    assert cfg.bench_dir == tmp_path / "стенд"
    assert Config().bench_dir.name == "bench"


def test_bench_sandbox_does_not_touch_the_real_voice_library(vault, tmp_path):
    """Эталон гоняется десятки раз: профиль владельца не пополняется, а метки не зависят от реальных голосов."""
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench",
                 voices_dir=tmp_path / "real-voices")
    _synthetic_bench(cfg.bench_dir)
    run = bench.run_bench(cfg, "synthetic", "голоса", overrides={"block_min_s": 1.0}, models=fake_models())
    used = yaml.safe_load((run / "config.yaml").read_text(encoding="utf-8"))
    assert used["owner_autolearn"] is False
    voices = Path(used["voices_dir"])
    assert voices != cfg.voices_dir and voices.parent.name.startswith("bench-synthetic-")
    assert not (tmp_path / "real-voices").exists()
