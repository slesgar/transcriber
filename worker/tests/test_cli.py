import json
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
from transcriber import cli, daemon
from transcriber.config import Config
from transcriber.voices import VoiceLibrary
from tests.conftest import FakeAsr, FakeEmbedder, SR, fake_prob, make_session, silence, to_pcm_bytes, tone


def write_wav(path: Path, audio: np.ndarray, sr: int = SR, channels: int = 1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        data = np.repeat(audio[:, None], channels, axis=1).reshape(-1) if channels > 1 else audio
        w.writeframes(to_pcm_bytes(data))


def fake_models():
    return daemon.Models(asr=FakeAsr(), embed=FakeEmbedder(), prob_factory=lambda: fake_prob)


def test_from_wav_runs_full_pipeline(vault, tmp_path):
    mic = tmp_path / "mic.wav"
    sys_ = tmp_path / "sys.wav"
    write_wav(mic, np.concatenate([tone(1.0, 440), silence(4.0)]))
    write_wav(sys_, np.concatenate([silence(2.0, sr=48000), tone(1.5, 880, sr=48000), silence(1.5, sr=48000)]), sr=48000, channels=2)
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    raw = cli.from_wav(cfg, mic, sys_, title="Из файла", app="manual", models=fake_models())
    text = raw.read_text()
    assert "Me: tone440" in text and "Speaker 1: tone880" in text and "финализирован" in text
    sdir = next((tmp_path / "sessions").iterdir())
    assert json.loads((sdir / "state.json").read_text())["status"] == "finalized"
    assert not (sdir / "mic.pcm").exists()


def test_daemon_once_processes_and_finalizes(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = make_session(sessions)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    cfg = Config(vault_root=vault, sessions_dir=sessions)
    dm = daemon.Daemon(cfg, models=fake_models())
    assert dm.once() is True
    assert json.loads((d / "state.json").read_text())["status"] == "recording"
    (d / "STOP").touch()
    dm.once()
    assert json.loads((d / "state.json").read_text())["status"] == "finalized"
    assert dm.once() is False        # finalized sessions are skipped


def test_daemon_releases_models_when_the_last_session_is_finalized(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = make_session(sessions)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    released = []
    models = fake_models()
    models.release = lambda: released.append(True)
    dm = daemon.Daemon(Config(vault_root=vault, sessions_dir=sessions), models=models)
    dm.once()
    assert released == []            # встреча идёт — кеш нужен
    (d / "STOP").touch()
    dm.once()
    assert released == [True]        # последняя встреча закрыта — отдали


def write_wav_width(path: Path, audio: np.ndarray, width: int, sr: int = SR):
    a = np.clip(audio, -1, 1)
    if width == 1:
        data = np.round(a * 127 + 128).astype(np.uint8).tobytes()
    elif width == 2:
        data = (a * 32767).astype("<i2").tobytes()
    elif width == 3:
        i32 = np.round(a * (2 ** 23 - 1)).astype("<i4")
        data = np.frombuffer(i32.tobytes(), dtype=np.uint8).reshape(-1, 4)[:, :3].tobytes()
    else:
        i32 = np.round(a * (2 ** 31 - 1)).astype("<i4")
        data = i32.tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(sr)
        w.writeframes(data)


def test_read_wav_supports_8_24_and_32_bit(tmp_path):
    audio = tone(0.5, 440)
    ref = cli.read_wav(_w(tmp_path, audio, 2), SR)
    for width, tol in ((1, 0.02), (3, 1e-4), (4, 1e-4)):
        got = cli.read_wav(_w(tmp_path, audio, width), SR)
        assert len(got) == len(ref)
        assert float(np.abs(got - ref).max()) < tol


def _w(tmp_path: Path, audio: np.ndarray, width: int) -> Path:
    p = tmp_path / f"w{width}.wav"
    write_wav_width(p, audio, width)
    return p


def test_from_wav_reports_unsupported_width_as_value_error(tmp_path, vault, capsys):
    import pytest

    mic = _wav_with_bits(tmp_path / "mic.wav", bits=40, frames=10)
    with pytest.raises(ValueError):
        cli.read_wav(mic, SR)
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    import transcriber.cli as climod

    monkey_cfg = cfg
    orig = climod.load_config
    climod.load_config = lambda *a, **k: monkey_cfg
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["from-wav", str(mic), str(mic)])
        assert e.value.code == 2
    finally:
        climod.load_config = orig
    assert "40-bit" in capsys.readouterr().err


def _wav_with_bits(path: Path, bits: int, frames: int, sr: int = SR) -> Path:
    """Модуль wave такой файл не запишет — собираем RIFF руками."""
    import struct

    width = bits // 8
    data = b"\x00" * (frames * width)
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, sr, sr * width, width, bits)
    body = b"WAVE" + fmt + struct.pack("<4sI", b"data", len(data)) + data
    path.write_bytes(struct.pack("<4sI", b"RIFF", len(body)) + body)
    return path


def test_from_wav_does_not_reuse_session_dir(vault, tmp_path):
    mic = tmp_path / "mic.wav"
    sys_ = tmp_path / "sys.wav"
    write_wav(mic, silence(1.0))
    write_wav(sys_, silence(1.0))
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    started = datetime(2026, 9, 15, 14, 30, 0).astimezone()
    cli.from_wav(cfg, mic, sys_, title="Раз", app="manual", models=fake_models(), started_at=started)
    cli.from_wav(cfg, mic, sys_, title="Два", app="manual", models=fake_models(), started_at=started)
    dirs = sorted(p.name for p in (tmp_path / "sessions").iterdir())
    assert dirs == ["2026-09-15-143000", "2026-09-15-143000-1"]


def test_daemon_backs_off_broken_session_and_keeps_others(vault, tmp_path, caplog):
    sessions = tmp_path / "sessions"
    broken = sessions / "0000-broken"
    broken.mkdir(parents=True)
    (broken / "meta.json").write_text("{ это не json")
    d = make_session(sessions)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    cfg = Config(vault_root=vault, sessions_dir=sessions)
    dm = daemon.Daemon(cfg, models=fake_models())
    for _ in range(5):
        dm.once()
    assert json.loads((d / "state.json").read_text())["records"]      # здоровая сессия обработана
    assert dm.failures[broken] == 5
    caplog.clear()
    with caplog.at_level("DEBUG"):
        for _ in range(3):
            dm.once()
    assert dm.failures[broken] == 5                                   # больше не трогаем
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_daemon_forgets_vanished_and_externally_finalized_sessions(vault, tmp_path):
    import shutil

    sessions = tmp_path / "sessions"
    d = make_session(sessions)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    cfg = Config(vault_root=vault, sessions_dir=sessions)
    dm = daemon.Daemon(cfg, models=fake_models())
    dm.once()
    assert d in dm.pipes
    state = json.loads((d / "state.json").read_text())
    state["status"] = "finalized"                     # финализировали снаружи, через cli finalize
    (d / "state.json").write_text(json.dumps(state))
    assert dm.once() is False
    assert d not in dm.pipes

    d2 = make_session(sessions, session_id="2026-09-15-1500", title="Вторая")
    (d2 / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d2 / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    dm.once()
    assert d2 in dm.pipes
    shutil.rmtree(d2)
    assert dm.once() is False
    assert d2 not in dm.pipes


def test_daemon_creates_vault_files_before_loading_models(vault, tmp_path, monkeypatch, caplog):
    """Клик «открыть заметку» в приложении не должен ждать 10 секунд загрузки моделей."""
    sessions = tmp_path / "sessions"
    d = make_session(sessions)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    cfg = Config(vault_root=vault, sessions_dir=sessions)

    def boom(_cfg):
        raise RuntimeError("модели не загрузились")

    monkeypatch.setattr(daemon, "load_models", boom)
    dm = daemon.Daemon(cfg)
    with caplog.at_level("ERROR", logger="transcriber"):
        assert dm.once() is False            # без моделей работы не сделано
    state = json.loads((d / "state.json").read_text())
    assert Path(state["raw_path"]).exists() and Path(state["note_path"]).exists()
    assert "Статус: идёт запись" in Path(state["raw_path"]).read_text()
    assert dm.failures[d] == 1               # падение ушло в обычный путь обработки, без краша


def test_daemon_does_not_rewrite_vault_files_for_a_resumed_session(vault, tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    d = make_session(sessions)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    cfg = Config(vault_root=vault, sessions_dir=sessions)
    daemon.Daemon(cfg, models=fake_models()).once()
    raw = Path(json.loads((d / "state.json").read_text())["raw_path"])
    raw.write_text("правка снаружи")
    monkeypatch.setattr(daemon, "load_models", lambda _cfg: (_ for _ in ()).throw(RuntimeError("no models")))
    dm = daemon.Daemon(cfg)
    dm.once()
    assert raw.read_text() == "правка снаружи"   # raw_path уже есть — заглушку не перезаписываем


SYNTHETIC_RECORDS = [
    {"channel": "sys", "start": 0.0, "end": 2.0, "text": "Давайте обсудим контракт", "speaker": "Speaker 1", "emb": 0},
    {"channel": "sys", "start": 4.0, "end": 6.0, "text": "Термины, Саша Громов.", "speaker": "Speaker 2", "emb": 1},
    {"channel": "sys", "start": 8.0, "end": 10.0, "text": "Мы отдадим SQL", "speaker": "Speaker 3", "emb": 2},
    {"channel": "sys", "start": 12.0, "end": 14.0, "text": "Аааа... Ааа...", "speaker": "Speaker 4", "emb": 3},
    {"channel": "sys", "start": 16.0, "end": 18.0, "text": "Ну да, конечно", "speaker": "Speaker 5", "emb": 4},
    {"channel": "mic", "start": 7.0, "end": 11.0, "text": "Мы отдадим SQL.", "speaker": "Me", "emb": None},
    {"channel": "mic", "start": 20.0, "end": 22.0, "text": "Я думаю, надо начать с октября", "speaker": "Me", "emb": None},
    {"channel": "mic", "start": 30.0, "end": 32.0, "text": "Owner, Ромашка, Омни, CDP, Анна Петрова.", "speaker": "Me", "emb": None},
]
SYNTHETIC_EMB = [[1, 0, 0], [0, 1, 0], [1, 0, 0], [0, 1, 0], [0.25, 0, 0.97]]


def _synthetic_session(sessions: Path) -> Path:
    d = make_session(sessions, title="Встреча")
    (d / "state.json").write_text(json.dumps({
        "status": "finalized", "mic_offset": 0, "sys_offset": 0, "raw_path": None, "note_path": None,
        "records": SYNTHETIC_RECORDS, "pending_mic": [],
        "embeddings": SYNTHETIC_EMB, "speaker_of_emb": [0, 1, 2, 3, 4], "errors": [],
    }, ensure_ascii=False))
    return d


def test_refinalize_cleans_texts_echo_and_speakers_without_audio(vault, tmp_path, caplog):
    sessions = tmp_path / "sessions"
    d = _synthetic_session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions,
                 glossary=["Ромашка", "Омни", "CDP", "Саша Громов", "Катя Зайцева"])
    with caplog.at_level("INFO", logger="transcriber"):
        raw = cli.refinalize(cfg, "2026-09-15-1430")

    state = json.loads((d / "state.json").read_text())
    assert state["speaker_of_emb"] == [0, 1, 0, 1, 0]         # пять «спикеров» схлопнулись в двух
    kinds = [(r["channel"], r["text"]) for r in state["records"]]
    assert ("mic", "Мы отдадим SQL.") not in kinds            # эхо убрано
    assert ("mic", "Я думаю, надо начать с октября") in kinds
    assert [r["text"] for r in state["records"] if r["channel"] == "sys"] == [
        "Давайте обсудим контракт", "", "Мы отдадим SQL", "", "Ну да, конечно"]
    assert [r["speaker"] for r in state["records"] if r["channel"] == "sys"] == [
        "Speaker 1", "Speaker 2", "Speaker 1", "Speaker 2", "Speaker 1"]

    body = raw.read_text().split("## Полный транскрипт", 1)[1]
    assert "Статус: финализирован повторно" in raw.read_text()
    assert "Громов" not in body and "Аааа" not in body
    assert body.count("Мы отдадим SQL") == 1
    assert "prompt leak" in caplog.text and "filler" in caplog.text


def test_refinalize_drops_acoustic_residue_by_kept(vault, tmp_path, caplog):
    sessions = tmp_path / "sessions"
    d = make_session(sessions, title="Встреча")
    records = [
        {"channel": "sys", "start": 0.0, "end": 10.0, "text": "Завтра встречаемся в половине второго",
         "speaker": "Speaker 1", "emb": 0},
        {"channel": "mic", "start": 2.0, "end": 5.0, "text": "мы с вами сегодня завтра встречаемся в 1-30",
         "speaker": "Me", "emb": None, "kept": 0.08},
        {"channel": "mic", "start": 6.0, "end": 7.0, "text": "А почему?", "speaker": "Me", "emb": None,
         "kept": 0.19},
        {"channel": "mic", "start": 12.0, "end": 13.0, "text": "Без доли", "speaker": "Me", "emb": None},
    ]
    (d / "state.json").write_text(json.dumps({"status": "finalized", "records": records,
                                              "embeddings": [[1, 0, 0]], "speaker_of_emb": [0]},
                                             ensure_ascii=False))
    cfg = Config(vault_root=vault, sessions_dir=sessions)
    with caplog.at_level("INFO", logger="transcriber"):
        cli.refinalize(cfg, "2026-09-15-1430")
    state = json.loads((d / "state.json").read_text())
    assert [r["text"] for r in state["records"] if r["channel"] == "mic"] == ["А почему?", "Без доли"]
    assert "echo residue (acoustic) dropped 2.0-5.0 kept=0.08" in caplog.text


def test_refinalize_is_idempotent(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = _synthetic_session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, glossary=["Ромашка", "Омни", "CDP", "Саша Громов"])
    first = cli.refinalize(cfg, "2026-09-15-1430").read_text()
    again = cli.refinalize(cfg, "2026-09-15-1430")
    assert again.read_text().replace("повторно", "") == first.replace("повторно", "")
    assert json.loads((d / "state.json").read_text())["speaker_of_emb"] == [0, 1, 0, 1, 0]


def test_refinalize_command_prints_raw_path(vault, tmp_path, capsys, monkeypatch):
    sessions = tmp_path / "sessions"
    _synthetic_session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, glossary=["Ромашка"])
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    cli.main(["refinalize", "2026-09-15-1430"])
    assert capsys.readouterr().out.strip().endswith("Local-2026-09-15-1430-Встреча.md")


ПЁТР = "Пётр Кузьмин"
КОМНАТА = "Переговорная 5"

# реплики по 2,5 с — длиннее рабочего порога min_embed_s (2 с), иначе отпечатки в обучение не идут
TALK_RECORDS = [{"channel": "sys", "start": float(3 * i), "end": float(3 * i) + 2.5,
                 "text": f"реплика {i}", "speaker": "Speaker 1", "emb": i} for i in range(5)]


def _talk_session(sessions: Path, vault: Path | None = None) -> Path:
    """Сессия без аудио: пять реплик одного голоса и talk.jsonl с плиткой Петра поверх них."""
    d = make_session(sessions, title="Встреча")
    (d / "state.json").write_text(json.dumps({
        "status": "finalized", "records": TALK_RECORDS, "pending_mic": [],
        "embeddings": [[1.0, 0.0, 0.0]] * 5, "speaker_of_emb": [0] * 5, "errors": [],
    }, ensure_ascii=False))
    (d / "talk.jsonl").write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in [
        {"t": 0.0, "participants": [{"name": ПЁТР, "mic": False, "active": True},
                                    {"name": КОМНАТА, "mic": True, "active": False}]},
        {"t": 20.0, "participants": [{"name": ПЁТР, "mic": False, "active": False},
                                     {"name": КОМНАТА, "mic": True, "active": True}]},
    ]) + "\n", encoding="utf-8")
    if vault is not None:                      # у записанной сессии MTG-заглушка уже есть
        from transcriber.session import Session
        from transcriber.vault import ensure_stub

        s = Session.load(d)
        ensure_stub(Config(vault_root=vault, sessions_dir=sessions), s)
        s.save_state()
    return d


def test_refinalize_recomputes_talk_names_from_the_saved_talk_file(vault, tmp_path):
    """talk.jsonl переживает сессию: имена пересчитываются даже когда аудио уже удалено."""
    sessions = tmp_path / "sessions"
    d = _talk_session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions)
    raw = cli.refinalize(cfg, "2026-09-15-1430")

    state = json.loads((d / "state.json").read_text())
    assert state["speaker_names"] == {"0": ПЁТР}
    assert [r["talk"] for r in state["records"]] == [ПЁТР] * 5       # в state.json их не было вовсе
    assert [r["speaker"] for r in state["records"]] == [ПЁТР] * 5
    text = raw.read_text()
    assert f"{ПЁТР}: реплика 0" in text
    assert f"Участники по данным Толка: {ПЁТР}, {КОМНАТА}." in text

    prof = VoiceLibrary(cfg.voices_dir).get(ПЁТР)
    assert prof is not None and prof.count == 5 and prof.sources == ["2026-09-15-1430:talk"]

    cli.refinalize(cfg, "2026-09-15-1430")                          # повтор не копит дубли
    again = VoiceLibrary(cfg.voices_dir).get(ПЁТР)
    assert again.count == 5 and len(again.samples) == 5


def test_refinalize_puts_the_talk_participants_into_the_stub(vault, tmp_path):
    sessions = tmp_path / "sessions"
    _talk_session(sessions, vault)
    cfg = Config(vault_root=vault, sessions_dir=sessions)
    cli.refinalize(cfg, "2026-09-15-1430")
    note = vault / "Memory/30 · Meetings/MTG-2026-09-15 1430 · Встреча.md"
    assert f"participants: [Owner, Иван Сидоров, {ПЁТР}, {КОМНАТА}]" in note.read_text()


def _session_with_audio(sessions: Path, vault: Path) -> tuple[Path, Path, Path]:
    """Сессия с сохранённым аудио и устаревшим state.json от прошлого прогона."""
    d = make_session(sessions)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 440), silence(5.0)])))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([silence(2.0), tone(1.5, 880), silence(2.0)])))
    raw = vault / "Memory/50 · Raw/Local/Local-2026-09-15-1430-Старое-имя.md"
    note = vault / "Memory/30 · Meetings/MTG-2026-09-15 1430 · Старое имя.md"
    raw.write_text("прошлый прогон")
    note.write_text("заглушка")
    (d / "state.json").write_text(json.dumps({
        "status": "finalized", "mic_offset": 10 ** 7, "sys_offset": 10 ** 7,
        "raw_path": str(raw), "note_path": str(note),
        "records": [{"channel": "sys", "start": 0.0, "end": 1.0, "text": "старый мусор",
                     "speaker": "Speaker 1", "emb": 0}],
        "pending_mic": [{"channel": "mic", "start": 0.0, "end": 1.0, "text": "хвост",
                         "speaker": "Me", "emb": None}],
        "embeddings": [[1.0, 0.0, 0.0]], "speaker_of_emb": [0], "errors": ["старая ошибка"],
    }, ensure_ascii=False))
    return d, raw, note


def test_retranscribe_reruns_pipeline_over_saved_audio(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d, raw, note = _session_with_audio(sessions, vault)
    cfg = Config(vault_root=vault, sessions_dir=sessions, keep_audio=True)
    out = cli.retranscribe(cfg, "2026-09-15-1430", models=fake_models())
    state = json.loads((d / "state.json").read_text())
    assert out == raw and state["raw_path"] == str(raw) and state["note_path"] == str(note)
    assert state["status"] == "finalized" and state["errors"] == []
    assert {r["text"] for r in state["records"]} == {"tone440", "tone880"}
    assert state["pending_mic"] == [] and state["mic_offset"] < 10 ** 7
    text = raw.read_text()
    assert "Me: tone440" in text and "Speaker 1: tone880" in text and "старый мусор" not in text
    assert (d / "mic.pcm").exists()                      # keep_audio: аудио остаётся под следующий прогон


def test_retranscribe_drops_audio_when_keep_audio_is_off(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d, _raw, _note = _session_with_audio(sessions, vault)
    cli.retranscribe(Config(vault_root=vault, sessions_dir=sessions), "2026-09-15-1430", models=fake_models())
    assert not (d / "mic.pcm").exists() and not (d / "sys.pcm").exists()


def test_retranscribe_command_prints_path_and_refuses_without_audio(vault, tmp_path, capsys, monkeypatch):
    import pytest

    sessions = tmp_path / "sessions"
    d, raw, _note = _session_with_audio(sessions, vault)
    cfg = Config(vault_root=vault, sessions_dir=sessions, keep_audio=True)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cli, "load_models", lambda _cfg: fake_models())
    cli.main(["retranscribe", "2026-09-15-1430"])
    assert capsys.readouterr().out.strip() == str(raw)

    (d / "mic.pcm").unlink()
    (d / "sys.pcm").unlink()
    with pytest.raises(SystemExit) as e:
        cli.main(["retranscribe", "2026-09-15-1430"])
    assert e.value.code == 2
    assert "аудио" in capsys.readouterr().err


# ---- голосовые профили: learn / voices ---------------------------------------


def _refinalized(vault, tmp_path) -> tuple[Config, Path]:
    """Синтетическая сессия после refinalize: кластер 0 — три эмбеддинга, кластер 1 — два."""
    sessions = tmp_path / "sessions"
    d = _synthetic_session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, glossary=["Ромашка"])
    cli.refinalize(cfg, "2026-09-15-1430")
    assert json.loads((d / "state.json").read_text())["speaker_of_emb"] == [0, 1, 0, 1, 0]
    return cfg, d


def test_learn_collects_cluster_embeddings_into_a_profile(vault, tmp_path, capsys):
    cfg, _d = _refinalized(vault, tmp_path)
    cli.learn(cfg, "2026-09-15-1430", {"Speaker 1": "Мария Иванова"})
    prof = VoiceLibrary(cfg.voices_dir).get("Мария Иванова")
    assert prof is not None and prof.count == 3 and len(prof.samples) == 3
    assert prof.sources == ["2026-09-15-1430:Speaker 1"]
    out = capsys.readouterr().out
    assert "Мария Иванова" in out and "Speaker 1" in out and "3" in out


def test_learn_accepts_a_cluster_that_is_already_named(vault, tmp_path):
    cfg, d = _refinalized(vault, tmp_path)
    state = json.loads((d / "state.json").read_text())
    state["speaker_names"] = {"0": "Ксения"}                      # так подписано в текущем Raw
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False))
    cli.learn(cfg, "2026-09-15-1430", {"Ксения": "Ксения Иванова"})
    assert VoiceLibrary(cfg.voices_dir).get("Ксения Иванова").count == 3


def test_learn_refuses_an_unknown_label_and_writes_nothing(vault, tmp_path):
    import pytest

    cfg, _d = _refinalized(vault, tmp_path)
    with pytest.raises(ValueError, match="Speaker 9"):
        cli.learn(cfg, "2026-09-15-1430", {"Speaker 1": "Мария Иванова", "Speaker 9": "Никто"})
    assert VoiceLibrary(cfg.voices_dir).names() == []            # проверка до записи, профилей нет


def test_learn_reads_the_mapping_from_an_mtg_note(vault, tmp_path):
    cfg, _d = _refinalized(vault, tmp_path)
    note = tmp_path / "MTG.md"
    note.write_text("\n".join([
        "---", "type: meeting", "id: MTG-2026-09-15-1430", "title: Встреча", "date: 2026-09-15",
        "participants: [Owner, Мария Иванова]",
        "speakers: {Speaker 1: Мария Иванова, Speaker 2: Ксения}",
        'transcript: "[[Local-2026-09-15-1430-Встреча]]"', "tags: [meeting]", "---", "", "# Встреча", "",
    ]), encoding="utf-8")
    assert cli.speakers_from_note(note) == {"Speaker 1": "Мария Иванова", "Speaker 2": "Ксения"}
    cli.learn(cfg, "2026-09-15-1430", cli.speakers_from_note(note))
    lib = VoiceLibrary(cfg.voices_dir)
    assert lib.names() == ["Ксения", "Мария Иванова"]
    assert lib.get("Мария Иванова").count == 3 and lib.get("Ксения").count == 2


def test_learn_command_and_voices_command(vault, tmp_path, capsys, monkeypatch):
    cfg, _d = _refinalized(vault, tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    cli.main(["learn", "2026-09-15-1430", "Speaker 1=Мария Иванова"])
    capsys.readouterr()
    cli.main(["voices"])
    out = capsys.readouterr().out
    assert "Мария Иванова" in out and "2026-09-15-1430:Speaker 1" in out and "3" in out

    cli.main(["voices"])                                          # пустая библиотека не падает
    VoiceLibrary(cfg.voices_dir).path_of("Мария Иванова").unlink()
    cli.main(["voices"])
    assert "пуст" in capsys.readouterr().out


def test_learn_command_reports_a_bad_pair_as_exit_2(vault, tmp_path, capsys, monkeypatch):
    import pytest

    cfg, _d = _refinalized(vault, tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    with pytest.raises(SystemExit) as e:
        cli.main(["learn", "2026-09-15-1430", "без знака равно"])
    assert e.value.code == 2 and "=" in capsys.readouterr().err


def test_refinalize_puts_learned_names_into_the_raw(vault, tmp_path):
    cfg, d = _refinalized(vault, tmp_path)
    cli.learn(cfg, "2026-09-15-1430", {"Speaker 1": "Мария Иванова"})
    raw = cli.refinalize(cfg, "2026-09-15-1430")
    text = raw.read_text()
    assert "Мария Иванова: Давайте обсудим контракт" in text
    assert "Опознаны по голосу: Мария Иванова" in text
    assert json.loads((d / "state.json").read_text())["speaker_names"] == {"0": "Мария Иванова"}


# ---- голосовые профили: forget -----------------------------------------------


def _learned(vault, tmp_path, monkeypatch):
    """Профиль «Мария Иванова» из двух источников: Speaker 1 — 3 отпечатка, Speaker 2 — 2."""
    cfg, _d = _refinalized(vault, tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    cli.main(["learn", "2026-09-15-1430", "Speaker 1=Мария Иванова", "Speaker 2=Мария Иванова"])
    return cfg


def test_voices_command_shows_counts_per_source(vault, tmp_path, capsys, monkeypatch):
    cfg = _learned(vault, tmp_path, monkeypatch)
    capsys.readouterr()
    cli.main(["voices"])
    out = capsys.readouterr().out
    assert "2026-09-15-1430:Speaker 1 — 3" in out and "2026-09-15-1430:Speaker 2 — 2" in out


def test_forget_command_deletes_the_whole_profile(vault, tmp_path, capsys, monkeypatch):
    cfg = _learned(vault, tmp_path, monkeypatch)
    capsys.readouterr()
    cli.main(["forget", "Мария Иванова"])
    assert "Профиль Мария Иванова удалён" in capsys.readouterr().out
    assert VoiceLibrary(cfg.voices_dir).names() == []
    assert not VoiceLibrary(cfg.voices_dir).path_of("Мария Иванова").exists()


def test_forget_source_command_reports_what_it_took_off(vault, tmp_path, capsys, monkeypatch):
    cfg = _learned(vault, tmp_path, monkeypatch)
    capsys.readouterr()
    cli.main(["forget", "Мария Иванова", "--source", "2026-09-15-1430:Speaker 1"])
    out = capsys.readouterr().out
    assert "снято 3" in out and "2026-09-15-1430:Speaker 1" in out and "осталось 2" in out
    prof = VoiceLibrary(cfg.voices_dir).get("Мария Иванова")
    assert prof.count == 2 and prof.sources == ["2026-09-15-1430:Speaker 2"]

    cli.main(["forget", "Мария Иванова", "--source", "2026-09-15-1430:Speaker 2"])
    out = capsys.readouterr().out
    assert "снято 2" in out and "осталось 0" in out and "удалён" in out
    assert VoiceLibrary(cfg.voices_dir).names() == []


def test_forget_command_on_an_unknown_name_exits_1(vault, tmp_path, capsys, monkeypatch):
    import pytest

    cfg = _learned(vault, tmp_path, monkeypatch)
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["forget", "Кого не учили"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "Кого не учили" in err and "Мария Иванова" in err        # подсказали, кто в библиотеке есть
    with pytest.raises(SystemExit) as e:
        cli.main(["forget", "Кого не учили", "--source", "2026-09-15-1430:Speaker 1"])
    assert e.value.code == 1
    assert VoiceLibrary(cfg.voices_dir).get("Мария Иванова").count == 5    # чужой профиль не тронут


def test_forget_source_counts_samples_whose_source_is_unknown(vault, tmp_path, capsys, monkeypatch):
    """Профиль старого формата: отпечатки без src по источнику не снимаются — об этом говорим вслух."""
    cfg = _learned(vault, tmp_path, monkeypatch)
    path = VoiceLibrary(cfg.voices_dir).path_of("Мария Иванова")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["samples"] = [s["v"] for s in data["samples"][:3]] + data["samples"][3:]   # первые три — по-старому
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    cli.main(["forget", "Мария Иванова", "--source", "2026-09-15-1430:Speaker 2"])
    out = capsys.readouterr().out
    assert "снято 2" in out and "осталось 3" in out
    assert "3" in out and "источник неизвестен" in out


def test_daemon_skips_a_session_locked_by_another_process(vault, tmp_path):
    import os, subprocess, sys
    sessions = tmp_path / "sessions"
    d = make_session(sessions)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(6.0)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(5.0)])))
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (d / "LOCK").write_text(str(other.pid))
        dm = daemon.Daemon(Config(vault_root=vault, sessions_dir=sessions), models=fake_models())
        assert dm.once() is False
        assert not (d / "state.json").exists()
    finally:
        other.kill(); other.wait()
    assert dm.once() is True                     # замок мёртвого процесса не считается


def test_session_lock_refuses_when_held_and_cleans_up(tmp_path):
    import subprocess, sys
    import pytest
    from transcriber.session import SessionLocked, session_lock
    d = make_session(tmp_path / "sessions")
    with session_lock(d):
        assert (d / "LOCK").exists()
    assert not (d / "LOCK").exists()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (d / "LOCK").write_text(str(other.pid))
        with pytest.raises(SessionLocked):
            with session_lock(d):
                pass
    finally:
        other.kill(); other.wait()


# ---- приватные записи: make-private ------------------------------------------


def test_daemon_puts_an_early_private_session_outside_the_vault(vault, tmp_path, private_dir, monkeypatch, caplog):
    """Заметка и транскрипт создаются до загрузки моделей — и у приватной сессии тоже мимо вольта."""
    from tests.conftest import vault_files

    sessions = tmp_path / "sessions"
    d = make_session(sessions, private=True)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    cfg = Config(vault_root=vault, sessions_dir=sessions, private_dir=private_dir)
    monkeypatch.setattr(daemon, "load_models", lambda _cfg: (_ for _ in ()).throw(RuntimeError("no models")))
    with caplog.at_level("ERROR", logger="transcriber"):
        daemon.Daemon(cfg).once()
    state = json.loads((d / "state.json").read_text())
    assert Path(state["raw_path"]) == private_dir / "2026-09-15-1430/transcript.md"
    assert Path(state["note_path"]) == private_dir / "2026-09-15-1430/notes.md"
    assert Path(state["raw_path"]).exists() and Path(state["note_path"]).exists()
    assert vault_files(vault) == []


def test_learn_refuses_a_private_session(vault, tmp_path, private_dir):
    import pytest

    sessions = tmp_path / "sessions"
    make_session(sessions, private=True)
    cfg = Config(vault_root=vault, sessions_dir=sessions, private_dir=private_dir)
    with pytest.raises(ValueError, match="приватн"):
        cli.learn(cfg, "2026-09-15-1430", {"Speaker 1": "Мария Иванова"})
    assert not VoiceLibrary(cfg.voices_dir).names()


def _recorded(vault, tmp_path, private_dir) -> tuple[Config, Path]:
    """Обычная записанная сессия: Raw и MTG-заглушка уже лежат в вольте."""
    from transcriber import vault as vault_mod

    sessions = tmp_path / "sessions"
    d = _synthetic_session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, private_dir=private_dir, glossary=["Ромашка"])
    session = cli.Session.load(d)
    vault_mod.ensure_stub(cfg, session)
    session.save_state()
    cli.refinalize(cfg, "2026-09-15-1430")
    return cfg, d


def _paths(d: Path) -> tuple[Path, Path]:
    state = json.loads((d / "state.json").read_text())
    return Path(state["raw_path"]), Path(state["note_path"])


def test_make_private_moves_the_raw_and_drops_an_untouched_stub(vault, tmp_path, private_dir):
    from tests.conftest import vault_files

    cfg, d = _recorded(vault, tmp_path, private_dir)
    old_raw, old_note = _paths(d)
    assert old_raw.exists() and old_note.exists()

    p = cli.make_private(cfg, "2026-09-15-1430")

    assert p == private_dir / "2026-09-15-1430/transcript.md"
    assert not old_raw.exists() and not old_note.exists()
    assert vault_files(vault) == []
    text = p.read_text()
    assert not text.startswith("---") and "Приватная запись. Вне рабочего вольта." in text
    assert "Я думаю, надо начать с октября" in text          # тело транскрипта сохранено
    assert json.loads((d / "meta.json").read_text())["private"] is True
    raw, note = _paths(d)
    assert raw == p and note == private_dir / "2026-09-15-1430/notes.md"
    assert note.read_text() == "# Заметки — 2026-09-15 14:30\n\n"
    assert (p.stat().st_mode & 0o777) == 0o600 and (note.stat().st_mode & 0o777) == 0o600


def test_make_private_carries_a_note_with_thoughts_out_of_the_vault(vault, tmp_path, private_dir):
    from tests.conftest import vault_files

    cfg, d = _recorded(vault, tmp_path, private_dir)
    _old_raw, old_note = _paths(d)
    old_note.write_text(old_note.read_text() + "\nтут мои мысли\n")

    cli.make_private(cfg, "2026-09-15-1430")

    note = private_dir / "2026-09-15-1430/notes.md"
    assert "тут мои мысли" in note.read_text()
    assert not old_note.exists() and vault_files(vault) == []
    assert (note.stat().st_mode & 0o777) == 0o600
    assert _paths(d)[1] == note


def test_make_private_refuses_a_session_that_is_already_private(vault, tmp_path, private_dir):
    import pytest

    cfg, _d = _recorded(vault, tmp_path, private_dir)
    cli.make_private(cfg, "2026-09-15-1430")
    with pytest.raises(ValueError, match="уже приватная"):
        cli.make_private(cfg, "2026-09-15-1430")


def test_make_private_command_prints_the_new_path(vault, tmp_path, private_dir, capsys, monkeypatch):
    import pytest

    cfg, _d = _recorded(vault, tmp_path, private_dir)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    cli.main(["make-private", "2026-09-15-1430"])
    assert capsys.readouterr().out.strip() == str(private_dir / "2026-09-15-1430/transcript.md")

    with pytest.raises(SystemExit) as e:
        cli.main(["make-private", "2026-09-15-1430"])
    assert e.value.code == 2 and "приватная" in capsys.readouterr().err


def test_make_private_purges_the_session_from_the_voice_library(vault, tmp_path, private_dir):
    """Приватная запись не должна оставаться в профилях: отпечатки mic и Толка этой сессии снимаются."""
    cfg, d = _recorded(vault, tmp_path, private_dir)
    sid = "2026-09-15-1430"
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    lib.add("Владелец", [[1.0, 0.0, 0.0]] * 3, f"{sid}:mic")
    lib.add("Владелец", [[1.0, 0.1, 0.0]] * 2, "другая:mic")
    lib.add("Ксения", [[0.0, 1.0, 0.0]] * 4, f"{sid}:talk")
    lib.add("Ксения", [[0.0, 1.0, 0.1]] * 2, "другая:talk")

    cli.make_private(cfg, sid)

    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    assert lib.get("Владелец").sources == ["другая:mic"] and lib.get("Владелец").count == 2
    assert lib.get("Ксения").sources == ["другая:talk"] and lib.get("Ксения").count == 2
