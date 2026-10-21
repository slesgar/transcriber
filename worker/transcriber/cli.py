"""CLI воркера.

* ``transcriber init`` — создать каталог данных из примеров и спросить минимум (владелец, его имя в Толке,
  вольт, папка приватных записей, хосты встреч); повторный запуск существующие файлы не трогает;
* ``transcriber run`` — демон, следит за каталогом сессий;
* ``transcriber from-wav MIC SYS`` — прогон пары WAV как одной сессии;
* ``transcriber finalize SESSION_ID`` — финализация сессии (нужны аудио и модели);
* ``transcriber refinalize SESSION_ID`` — постобработка уже сохранённого ``state.json``
  без аудио и без ASR: тексты через фильтры галлюцинаций, эхо-фильтр по всем mic-репликам,
  перекластеризация эмбеддингов текущими порогами, имена из ``talk.jsonl`` заново (плитки
  Толка переживают сессию), перезапись Raw. Нужна, когда пороги или фильтры поменялись,
  а переслушать встречу уже нечем — аудио удалено;
* ``transcriber retranscribe SESSION_ID`` — полный повторный прогон сессии по сохранённому
  ``mic.pcm``/``sys.pcm``: state сбрасывается (Raw и MTG остаются на своих путях) и вся
  дорожка проходит ASR заново с текущим конфигом. Нужна при ``keep_audio: true``, когда
  поменялись промпт, модель или пороги сегментации;
* ``transcriber make-private SESSION_ID`` — перевести уже записанную сессию в приватные:
  в ``meta.json`` проставляется ``private: true``, транскрипт переезжает из вольта в
  ``private_dir/<id>/transcript.md`` (перерисовывается без фронт-маттера вольта), MTG-заглушка
  из вольта сносится, если в неё ещё не писали, а заметка с мыслями уезжает туда же
  ``notes.md``. Каталог и файлы — только для владельца (0700/0600). Нужна, когда приложение
  не пометило приватную встречу при старте и она уже записалась в рабочий вольт;
* ``transcriber learn SESSION_ID "Speaker 2=Анна Петрова" …`` — положить отпечатки голоса
  кластера в библиотеку под именем человека. Метка берётся из финального Raw: ``Speaker N``
  или уже проставленное имя. Вместо пар можно указать ``--from-note ПУТЬ`` — ключ
  ``speakers:`` из фронт-маттера MTG-заметки. После ``learn`` полезен ``refinalize``:
  он переподпишет реплики именами;
* ``transcriber voices`` — что накоплено в библиотеке голосов: имя, число отпечатков,
  источники и сколько отпечатков принёс каждый;
* ``transcriber forget "Анна Петрова"`` — снести профиль целиком;
  ``transcriber forget "Анна Петрова" --source 2026-09-15-1622:Speaker 2`` — снять только
  отпечатки одной метки одной сессии, когда ``learn`` отдал кластер не тому человеку.
  Отпечатки профилей старого формата (без источника) снимаются только вместе с профилем;
* ``transcriber bench 2026-09-21-1030 --label current-main [--set block_min_s=30 …] [--note "…"]``
  — стенд качества: прогнать эталонную запись из ``bench/<id>/`` текущим кодом (тот же путь, что
  у ``retranscribe``) во временной сессии и временном вольте и положить транскрипт, метрики,
  конфиг, коммит и лог в ``bench/<id>/runs/NN-<label>/``. Рабочий вольт, рабочие сессии и
  библиотека голосов не трогаются. После прогона печатается таблица по всем прогонам записи;
* ``transcriber bench-diar --label current-main [--set speaker_similarity=0.45] [--since 2026-10-01]``
  — стенд диаризации: перекластеризовать сохранённые эмбеддинги записанных сессий текущим конфигом
  и сверить кластеры с плитками Толка (чистота, цельность, склейки, расколы); аудио и модели не
  нужны, сессии только читаются, результат в ``bench/diar/runs/NN-<label>/`` (bench_diar.py);
* ``transcriber bench-ref --label current [--since 2026-10-01] [--judge]`` — сравнение наших Raw с транскриптами
  бота Толка (заметки бота в вольте) по встречам, записанным обоими: общие слова, покрытие,
  согласие говорящих и имён, куски только у одной стороны; результат в ``bench/ref/runs/NN-<label>/``
  (bench_ref.py);
* ``transcriber bench-diff 2026-09-21-1030 01-prompt-and-filters 02-current-main`` — пословное
  сравнение двух прогонов: доля совпавшего в обе стороны и самые длинные куски, которые есть
  только в одном из них, с ближайшим маркером времени. Прогон можно назвать номером («01»).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

from . import bench as bench_mod
from . import bench_diar as diar_mod
from . import bench_ref as ref_mod
from . import config as config_module
from .config import Config, load_config
from .daemon import Daemon
from .echo import is_acoustic_residue, is_echo, is_residue
from .firstrun import Answers, ask, init_data_dir
from .models import Models, load_models
from .pipeline import Pipeline, drop_repeated_mic_residue, recluster_records
from .quality import junk_reason, prompt_vocabulary, strip_leak_prefix
from .speakers import label
from .session import Session, SessionLocked, State, atomic_write_text, session_lock
from .talk import TalkFile
from .agent_qa import qa_path_for_raw, write_agent_qa_safely
from .vault import (ensure_stub, is_untouched_stub, note_path, update_stub_participants, write_raw,
                    write_text)
from .voices import VoiceLibrary, VoiceProfile

log = logging.getLogger("transcriber")


def decode_pcm(raw: bytes, width: int) -> np.ndarray:
    """PCM-сэмплы WAV в float32 [-1, 1]. 8 бит — беззнаковые, 24 бита — три байта little-endian."""
    if width == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if width == 3:
        b = np.frombuffer(raw, dtype=np.uint8)
        b = b[: len(b) // 3 * 3].reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)          # знаковое расширение
        return v.astype(np.float32) / float(1 << 23)
    if width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    raise ValueError(f"unsupported WAV sample width: {width * 8}-bit")


def read_wav(path: Path, target_sr: int) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    try:
        audio = decode_pcm(raw, width)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if sr != target_sr:
        from scipy.signal import resample_poly

        g = math.gcd(sr, target_sr)
        audio = resample_poly(audio, target_sr // g, sr // g).astype(np.float32)
    return audio


def from_wav(cfg: Config, mic: Path, sys_: Path, title: str, app: str, models: Models | None = None,
             started_at: datetime | None = None) -> Path:
    started_at = started_at or datetime.now().astimezone()
    base = started_at.strftime("%Y-%m-%d-%H%M%S")
    sid, n = base, 0
    while (cfg.sessions_dir / sid / "meta.json").exists():   # чужую сессию не переписываем
        n += 1
        sid = f"{base}-{n}"
    d = cfg.sessions_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": sid, "started_at": started_at.isoformat(), "app": app,
                                             "title": title, "participants": []}, ensure_ascii=False))
    for name, src in (("mic.pcm", mic), ("sys.pcm", sys_)):
        (d / name).write_bytes((np.clip(read_wav(src, cfg.sample_rate), -1, 1) * 32767).astype(np.int16).tobytes())
    (d / "STOP").touch()
    m = models or load_models(cfg)
    pipe = Pipeline(cfg, Session.load(d), m.asr, m.embed, m.prob_factory)
    pipe.finalize()
    return Path(pipe.s.state.raw_path)


def refinalize(cfg: Config, session_id: str) -> Path:
    """Пересобрать результат сессии из сохранённого state.json — без аудио, без ASR."""
    with session_lock(cfg.sessions_dir / session_id):
        return _refinalize(cfg, session_id)


def _refinalize(cfg: Config, session_id: str) -> Path:
    session = Session.load(cfg.sessions_dir / session_id)
    st = session.state
    vocabulary = prompt_vocabulary(session.meta.participants, cfg.glossary, cfg.owner_name)
    for r in st.records:
        if r.text:
            r.text = strip_leak_prefix(r.text)
        reason = junk_reason(r.text, vocabulary, cfg.language) if r.text else None
        if reason:
            log.info("dropped (%s) %.1f-%.1f: %s", reason, r.start, r.end, r.text)
            r.text = ""
    sys_recs = [r for r in st.records if r.channel == "sys"]
    kept: list = []
    for r in st.records:
        if r.channel == "mic" and is_echo(r, sys_recs, cfg.echo_overlap, cfg.echo_similarity,
                                          cfg.echo_strong_similarity):
            log.info("echo dropped %.1f-%.1f: %s", r.start, r.end, r.text)
            continue
        if r.channel == "mic" and is_residue(r, sys_recs):
            log.info("echo residue dropped %.1f-%.1f: %s", r.start, r.end, r.text)
            continue
        if r.channel == "mic" and is_acoustic_residue(r, sys_recs, cfg.echo_kept_max,
                                                      cfg.echo_kept_min_cover):
            log.info("echo residue (acoustic) dropped %.1f-%.1f kept=%.2f: %s", r.start, r.end, r.kept, r.text)
            continue
        kept.append(r)
    st.records = kept
    drop_repeated_mic_residue(cfg, st)
    # имена из Толка пересчитываются от исходных плиток: talk.jsonl переживает сессию, а пороги
    # и шаблоны могли поменяться — ровно та же логика, что на финализации
    talk = TalkFile(cfg.sessions_dir / session_id / "talk.jsonl")
    recluster_records(cfg, st, private=session.meta.private, timeline=talk.timeline,
                      session_id=session_id)
    participants = [] if session.meta.private else talk.participants(cfg.talk_bot_patterns)
    p = write_raw(cfg, session, f"финализирован повторно {datetime.now().strftime('%H:%M')}", participants)
    update_stub_participants(cfg, session, participants)
    write_agent_qa_safely(cfg, session)
    session.save_state()
    return p


def retranscribe(cfg: Config, session_id: str, models: Models | None = None) -> Path:
    """Прогнать сессию заново по сохранённому аудио — с текущим промптом, моделью и порогами."""
    d = cfg.sessions_dir / session_id
    with session_lock(d):
        return _retranscribe(cfg, d, session_id, models)


def _retranscribe(cfg: Config, d: Path, session_id: str, models: Models | None) -> Path:
    session = Session.load(d)
    if not (session.mic_pcm.exists() or session.sys_pcm.exists()):
        raise ValueError(f"{session_id}: аудио сессии не сохранено — помогает только refinalize")
    st = session.state
    st.status = "recording"                    # raw_path и note_path сохраняем: файлы вольта те же
    st.mic_offset = st.sys_offset = 0
    st.records, st.pending_mic, st.embeddings, st.speaker_of_emb, st.errors = [], [], [], [], []
    st.mic_embeddings, st.mic_cluster_of_emb, st.mic_speaker_names = [], [], {}
    session.save_state()
    (d / "STOP").touch()
    m = models or load_models(cfg)
    pipe = Pipeline(cfg, session, m.asr, m.embed, m.prob_factory)
    while pipe.step() or pipe.has_backlog():
        pass
    pipe.finalize()
    return Path(pipe.s.state.raw_path)


def make_private(cfg: Config, session_id: str) -> Path:
    """Перевести уже записанную сессию в приватные: файлы — из вольта в ``private_dir``."""
    with session_lock(cfg.sessions_dir / session_id):
        return _make_private(cfg, session_id)


def _make_private(cfg: Config, session_id: str) -> Path:
    d = cfg.sessions_dir / session_id
    session = Session.load(d)
    if session.meta.private:
        raise ValueError(f"{session_id}: сессия уже приватная")
    if session.state.continues or session.state.continued_by:
        raise ValueError(f"{session_id}: часть встречи из нескольких записей (общий Raw с "
                         f"{session.state.continues or session.state.continued_by}) — в приватные переводятся "
                         "только целые встречи; сначала разорвать связь в state.json обеих частей")
    old_raw = Path(session.state.raw_path) if session.state.raw_path else None
    old_note = Path(session.state.note_path) if session.state.note_path else None
    meta_file = d / "meta.json"
    data = json.loads(meta_file.read_text())
    data["private"] = True
    atomic_write_text(meta_file, json.dumps(data, ensure_ascii=False))
    session.meta.private = True
    # отпечатки этой сессии убираем из библиотеки: приватный разговор не должен оставаться в профилях
    lib, removed = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity), 0
    for name in lib.names():
        for suffix in ("mic", "talk"):
            removed += lib.forget_source(name, f"{session_id}:{suffix}")
    if removed:
        log.info("%s: из библиотеки голосов снято %d отпечатков", session_id, removed)
    session.state.raw_path = None            # путь фиксируется заново, уже в приватном каталоге
    session.state.note_path = None
    _carry_note(cfg, session, old_note)
    session.save_state()
    p = _refinalize(cfg, session_id)         # транскрипт перерисовывается без фронт-маттера вольта
    if old_raw is not None and old_raw.exists() and old_raw != p:
        old_raw.unlink()
    if old_raw is not None:   # вопросы агенту к теперь приватной встрече в вольте не остаются
        qa = qa_path_for_raw(old_raw)
        if qa.exists() and qa.parent != p.parent:
            qa.unlink()
    log.info("%s: приватная запись, файлы в %s", session_id, p.parent)
    return p


def _carry_note(cfg: Config, session: Session, old_note: Path | None) -> None:
    """Заглушку из вольта сносим, заметку с мыслями уносим целиком в приватный каталог.

    Сносим только нетронутую заглушку: чужой текст удалять нельзя, даже свой.
    """
    if old_note is not None and old_note.exists() and not is_untouched_stub(
            old_note.read_text(encoding="utf-8"), cfg.owner_thoughts_heading):
        dest = note_path(cfg, session.meta)
        write_text(cfg, session.meta, dest, old_note.read_text(encoding="utf-8"))
        old_note.unlink()
        session.state.note_path = str(dest)
        return
    if old_note is not None:
        old_note.unlink(missing_ok=True)
    ensure_stub(cfg, session)


SPEAKER_LABEL = re.compile(r"Speaker\s+(\d+)$")


def parse_pair(text: str) -> tuple[str, str]:
    """«Speaker 2=Анна Петрова» → («Speaker 2», «Анна Петрова»)."""
    label, sep, name = text.partition("=")
    if not sep or not label.strip() or not name.strip():
        raise ValueError(f'{text!r}: нужна пара вида "Speaker 2=Анна Петрова" — со знаком =')
    return label.strip(), name.strip()


def speakers_from_note(path: Path) -> dict[str, str]:
    """Ключ ``speakers:`` из фронт-маттера MTG-заметки: {«Speaker 2»: «Анна Петрова»}."""
    text = Path(path).read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if not text.lstrip().startswith("---") or len(parts) < 3:
        raise ValueError(f"{path}: нет фронт-маттера")
    block = parts[1]
    try:
        data = yaml.safe_load(block) or {}
    except yaml.YAMLError:
        # чужие ключи заметки могут не пережить safe_load — нам нужна ровно одна строка
        line = next((x for x in block.splitlines() if x.startswith("speakers:")), "")
        data = yaml.safe_load(line) if line else {}
    speakers = (data or {}).get("speakers")
    if not isinstance(speakers, dict) or not speakers:
        raise ValueError(f"{path}: во фронт-маттере нет ключа speakers с парами «Speaker N: Имя»")
    return {str(k).strip(): str(v).strip() for k, v in speakers.items()}


def cluster_of_label(st: State, text: str) -> int | None:
    """Метка из финального Raw в индекс кластера: «Speaker N» → N-1, имя → его кластер."""
    m = SPEAKER_LABEL.match(text)
    if m:
        return int(m.group(1)) - 1
    for key, name in st.speaker_names.items():
        if name == text:
            return int(key)
    return None


def learn(cfg: Config, session_id: str, mapping: dict[str, str]) -> list[tuple[str, str, int]]:
    """Отпечатки названных кластеров сессии — в библиотеку голосов.

    Сверка идёт по всей карте до первой записи: половина применённого сопоставления хуже,
    чем понятная ошибка и повтор команды целиком.
    """
    if not mapping:
        raise ValueError("нечего учить: не задана ни одна пара «Speaker N=Имя»")
    session = Session.load(cfg.sessions_dir / session_id)
    if session.meta.private:
        raise ValueError(f"{session_id}: сессия приватная — голоса из неё в библиотеку не берём")
    st = session.state
    known = sorted({label(c) for c in st.speaker_of_emb} | set(st.speaker_names.values()))
    plan: list[tuple[str, str, list[list[float]]]] = []
    for text, name in mapping.items():
        cluster = cluster_of_label(st, text)
        embs = [st.embeddings[i] for i, c in enumerate(st.speaker_of_emb[:len(st.embeddings)])
                if int(c) == cluster] if cluster is not None else []
        if not embs:
            raise ValueError(f"{session_id}: метки {text!r} в сессии нет; есть {', '.join(known) or '—'}")
        plan.append((text, name, embs))
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    out: list[tuple[str, str, int]] = []
    for text, name, embs in plan:
        prof = lib.add(name, embs, source=f"{session_id}:{text}")
        print(f"{name} ← {text}: +{len(embs)} отпечатков, всего {prof.count}")
        out.append((name, text, len(embs)))
    return out


UNKNOWN_SOURCE = "источник неизвестен"


def sources_line(prof: VoiceProfile) -> str:
    """«2026-09-15-1622:Speaker 2 — 30 · источник неизвестен — 5»."""
    parts = [f"{src or UNKNOWN_SOURCE} — {n}" for src, n in prof.by_source().items()]
    return " · ".join(parts) or "источник не указан"


def show_voices(cfg: Config) -> None:
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    names = lib.names()
    if not names:
        print(f"Библиотека голосов пуста ({cfg.voices_dir})")
        return
    print(f"Библиотека голосов ({cfg.voices_dir}), порог {cfg.voice_match_similarity}:")
    for name in names:
        prof = lib.get(name)
        print(f"  {name}: {prof.count} отпечатков ({len(prof.samples)} хранится) · {sources_line(prof)}")


def forget(cfg: Config, name: str, source: str | None = None) -> None:
    """Убрать из библиотеки профиль целиком или отпечатки одного источника.

    Неизвестное имя — ошибка (``LookupError``): молча удалить не то имя хуже, чем переспросить.
    """
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    prof = lib.get(name)
    if prof is None:
        raise LookupError(f"{name}: такого профиля в библиотеке нет; есть {', '.join(lib.names()) or '—'}")
    if source is None:
        lib.forget(name)
        print(f"Профиль {name} удалён")
        return
    unknown = prof.by_source().get(None, 0)          # считаем до правки: forget_source меняет профиль на месте
    removed = lib.forget_source(name, source)
    if not removed:
        print(f"{name}: отпечатков из {source} в профиле нет · {sources_line(prof)}")
    else:
        rest = lib.get(name)
        left = f"осталось {len(rest.samples)}" if rest else "осталось 0 → профиль удалён"
        print(f"{name}: снято {removed} отпечатков из {source}, {left}")
    if unknown and lib.get(name) is not None:
        print(f"  у {unknown} отпечатков {UNKNOWN_SOURCE} (профиль записан старым форматом) — "
              f"по источнику их не снять, только forget без --source")


def init_command(args: argparse.Namespace) -> None:
    """``transcriber init``: флаги — умолчания вопросов, ``--yes`` вопросы пропускает."""
    base = Answers()
    for attr, value in (("owner_name", args.owner), ("owner_talk_name", args.owner_talk_name),
                        ("vault_root", args.vault), ("private_dir", args.private_dir)):
        if value is not None:
            setattr(base, attr, value)
    if args.hosts is not None:
        base.meeting_hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    answers = base if args.yes else ask(base)
    data_dir = config_module.DATA_DIR            # глобал модуля, читаем в момент вызова
    created = init_data_dir(data_dir, answers)
    if created:
        print(f"Каталог данных: {data_dir}")
        print("Создано: " + ", ".join(created))
    else:
        print(f"уже настроено: {data_dir}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="transcriber")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="daemon: watch sessions dir")
    fw = sub.add_parser("from-wav", help="transcribe two WAV files as one session")
    fw.add_argument("mic")
    fw.add_argument("sys")
    fw.add_argument("--title", default="")
    fw.add_argument("--app", default="manual")
    fz = sub.add_parser("finalize", help="finalize a session by id")
    fz.add_argument("session_id")
    rf = sub.add_parser("refinalize", help="redo post-processing over a saved session (no audio, no ASR)")
    rf.add_argument("session_id")
    rt = sub.add_parser("retranscribe", help="run the whole pipeline again over the session's saved audio")
    rt.add_argument("session_id")
    mp = sub.add_parser("make-private", help="move an already recorded session out of the vault")
    mp.add_argument("session_id")
    ln = sub.add_parser("learn", help='teach a voice: learn SESSION_ID "Speaker 2=Анна Петрова" …')
    ln.add_argument("session_id")
    ln.add_argument("pairs", nargs="*", metavar="SPEAKER=NAME")
    ln.add_argument("--from-note", metavar="PATH", help="взять карту из ключа speakers: фронт-маттера MTG-заметки")
    sub.add_parser("voices", help="list the voice library")
    fg = sub.add_parser("forget", help='forget a voice: forget "Анна Петрова" [--source SESSION:LABEL]')
    fg.add_argument("name", metavar="NAME")
    fg.add_argument("--source", metavar="SESSION:LABEL",
                    help="снять только отпечатки этой метки этой сессии, а не весь профиль")
    bn = sub.add_parser("bench", help="run the bench audio through the current code and score it")
    bn.add_argument("bench_id")
    bn.add_argument("--label", required=True, help="метка прогона: current-main, no-prompt, …")
    bn.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                    help="переопределить поле конфига на этот прогон; можно несколько раз")
    bn.add_argument("--note", help="чем этот прогон отличается — ляжет в note.txt")
    bdr = sub.add_parser("bench-diar", help="score the clustering of recorded sessions against the Talk tiles")
    bdr.add_argument("--label", required=True, help="метка прогона: current-main, sim-045, …")
    bdr.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                     help="переопределить поле конфига на этот прогон (speaker_similarity, min_embed_s, …)")
    bdr.add_argument("--note", help="чем этот прогон отличается — ляжет в note.txt")
    bdr.add_argument("--since", metavar="YYYY-MM-DD", help="только сессии с этой даты")
    bdr.add_argument("--sessions", metavar="ID,ID", help="только эти сессии")
    bdr.add_argument("--min-labeled", type=int, default=diar_mod.MIN_LABELED, metavar="N",
                     help="минимум размеченных реплик в сессии")
    bdr.add_argument("--min-names", type=int, default=diar_mod.MIN_NAMES, metavar="N",
                     help="минимум имён плиток в сессии")
    bdr.add_argument("--holdout", action="store_true",
                     help="разрез по плиткам — по чётным репликам, метрика — по нечётным (честная проверка тикета 04)")
    brf = sub.add_parser("bench-ref", help="compare our transcripts with the Talk bot's notes  for the same meetings")
    brf.add_argument("--label", required=True, help="метка прогона")
    brf.add_argument("--sessions", metavar="ID,ID", help="только эти сессии")
    brf.add_argument("--since", metavar="YYYY-MM-DD", help="только сессии с этой даты")
    brf.add_argument("--note", help="чем этот прогон отличается — ляжет в note.txt")
    brf.add_argument("--judge", action="store_true",
                     help="оценить контекст общего окна моделью Claude (ключ ANTHROPIC_API_KEY в .env каталога данных)")
    bd = sub.add_parser("bench-diff", help="compare the transcripts of two bench runs word by word")
    bd.add_argument("bench_id")
    bd.add_argument("run_a", metavar="RUN_A")
    bd.add_argument("run_b", metavar="RUN_B")
    ini = sub.add_parser("init", help="create the data dir from the examples and ask the minimum")
    ini.add_argument("--yes", action="store_true", help="не спрашивать, взять умолчания и флаги")
    ini.add_argument("--owner", metavar="NAME", help="имя владельца")
    ini.add_argument("--owner-talk-name", metavar="NAME", help="имя владельца в сетке Толка")
    ini.add_argument("--vault", metavar="PATH", help="папка вольта Obsidian")
    ini.add_argument("--private-dir", metavar="PATH", help="папка приватных записей")
    ini.add_argument("--hosts", metavar="a,b", help="хосты рабочих звонков в браузере через запятую")
    args = ap.parse_args(argv)
    if args.cmd == "init":
        init_command(args)
        return
    cfg = load_config()
    if args.cmd == "run":
        Daemon(cfg, reload_config=load_config).run_forever()
    elif args.cmd == "from-wav":
        try:
            print(from_wav(cfg, Path(args.mic), Path(args.sys), args.title, args.app))
        except (ValueError, SessionLocked) as e:
            print(e, file=sys.stderr)
            raise SystemExit(2) from None
    elif args.cmd == "finalize":
        d = cfg.sessions_dir / args.session_id
        (d / "STOP").touch()
        m = load_models(cfg)
        pipe = Pipeline(cfg, Session.load(d), m.asr, m.embed, m.prob_factory)
        pipe.finalize()
        print(pipe.s.state.raw_path)
    elif args.cmd == "refinalize":
        print(refinalize(cfg, args.session_id))
    elif args.cmd == "retranscribe":
        try:
            print(retranscribe(cfg, args.session_id))
        except ValueError as e:
            print(e, file=sys.stderr)
            raise SystemExit(2) from None
    elif args.cmd == "make-private":
        try:
            print(make_private(cfg, args.session_id))
        except (ValueError, OSError, SessionLocked) as e:
            print(e, file=sys.stderr)
            raise SystemExit(2) from None
    elif args.cmd == "learn":
        try:
            mapping = speakers_from_note(Path(args.from_note)) if args.from_note else {}
            mapping.update(dict(parse_pair(x) for x in args.pairs))
            learn(cfg, args.session_id, mapping)
        except (ValueError, OSError) as e:
            print(e, file=sys.stderr)
            raise SystemExit(2) from None
        print("готово; чтобы имена попали в Raw — transcriber refinalize " + args.session_id)
    elif args.cmd == "voices":
        show_voices(cfg)
    elif args.cmd == "forget":
        try:
            forget(cfg, args.name, args.source)
        except LookupError as e:
            print(e, file=sys.stderr)
            raise SystemExit(1) from None
    elif args.cmd == "bench":
        try:
            overrides = bench_mod.parse_overrides(args.overrides, cfg)
            run = bench_mod.run_bench(cfg, args.bench_id, args.label, overrides, args.note)
        except (ValueError, OSError) as e:
            print(e, file=sys.stderr)
            raise SystemExit(2) from None
        print(run)
        print(bench_mod.format_table(bench_mod.collect_rows(run.parent)))
    elif args.cmd == "bench-diar":
        try:
            overrides = bench_mod.parse_overrides(args.overrides, cfg)
            ids = [x.strip() for x in (args.sessions or "").split(",") if x.strip()] or None
            run = diar_mod.run_bench_diar(cfg, args.label, overrides, args.note, since=args.since,
                                          sessions=ids, min_labeled=args.min_labeled, min_names=args.min_names,
                                          holdout=args.holdout)
        except (ValueError, OSError) as e:
            print(e, file=sys.stderr)
            raise SystemExit(2) from None
        print(run)
        print(diar_mod.format_table(diar_mod.collect_rows(run.parent)))
    elif args.cmd == "bench-ref":
        try:
            ids = [x.strip() for x in (args.sessions or "").split(",") if x.strip()] or None
            client = None
            if args.judge:
                from . import judge as judge_mod
                if not judge_mod.has_credentials():
                    raise ValueError("bench-ref --judge: нет ANTHROPIC_API_KEY (положи в .env каталога данных)")
                client = judge_mod.anthropic_client(cfg)
            run = ref_mod.run_bench_ref(cfg, args.label, sessions=ids, since=args.since, note=args.note, client=client)
        except (ValueError, OSError) as e:
            print(e, file=sys.stderr)
            raise SystemExit(2) from None
        print(run)
        rows = json.loads((run / "metrics.json").read_text(encoding="utf-8"))["sessions"]
        print(ref_mod.session_rows(rows))
        print()
        print(ref_mod.format_table(ref_mod.collect_rows(run.parent)))
    elif args.cmd == "bench-diff":
        try:
            print(bench_mod.diff_runs(cfg, args.bench_id, args.run_a, args.run_b))
        except (ValueError, OSError) as e:
            print(e, file=sys.stderr)
            raise SystemExit(2) from None


if __name__ == "__main__":
    main(sys.argv[1:])
