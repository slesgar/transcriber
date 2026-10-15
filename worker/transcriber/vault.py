from __future__ import annotations

import os
import re
from pathlib import Path

from .config import Config
from .session import Meta, Rec, Session, atomic_write_text

APPS: dict[str, tuple[str, str]] = {"kontur.talk": ("Толк", "Толке"), "zoom.us": ("Zoom", "Zoom")}

TITLE_CAP = 120

PRIVATE_DIR_MODE = 0o700        # приватный каталог и файлы видны только владельцу
PRIVATE_FILE_MODE = 0o600
PRIVATE_HEADER = "Приватная запись. Вне рабочего вольта."
THOUGHTS = "## Мысли владельца"
STUB_TAG = re.compile(r"^tags:.*\bstub\b", re.M)
FRONT_PARTICIPANTS = re.compile(r"^participants:.*$", re.M)
BODY_PARTICIPANTS = re.compile(r"^(\*\*Дата:\*\*.*\*\*Участники:\*\* ).*$", re.M)


def app_label(app: str) -> str:
    pair = APPS.get(app)
    return pair[0] if pair else ""


def app_locative(app: str) -> str:
    pair = APPS.get(app)
    return pair[1] if pair else ""


def default_title(meta: Meta) -> str:
    name = app_locative(meta.app)
    return f"Встреча в {name}" if name else "Встреча"


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[/\\:|#^\[\]]", " ", title)).strip()


def title_of(meta: Meta) -> str:
    return meta.title.strip() if clean_title(meta.title) else default_title(meta)


def slug(title: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"\s+", "-", clean_title(title))).strip("-")


def stamp(meta: Meta) -> tuple[str, str]:
    return meta.started_at.strftime("%Y-%m-%d"), meta.started_at.strftime("%H%M")


def raw_filename(meta: Meta) -> str:
    date, hhmm = stamp(meta)
    return f"Local-{date}-{hhmm}-{slug(title_of(meta))[:TITLE_CAP]}.md"


def note_filename(meta: Meta) -> str:
    date, hhmm = stamp(meta)
    return f"MTG-{date} {hhmm} · {clean_title(title_of(meta))[:TITLE_CAP]}.md"


def mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def hhmm(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def render_raw(meta: Meta, records: list[Rec], n_speakers: int, status_text: str, marker_every_s: int = 300,
               identified: list[str] | None = None, talk_participants: list[str] | None = None,
               owner: str = "Owner", mic_guests: list[str] | None = None,
               breaks: list[tuple[float, float]] | None = None) -> str:
    date, _ = stamp(meta)
    title = title_of(meta)
    who = ", ".join(meta.participants) if meta.participants else "не получены"
    app = app_label(meta.app) or "ручной старт"
    # у приватной записи нет фронт-маттера вольта: файл лежит вне вольта и в граф не попадает
    front = [] if meta.private else [
        "---", "type: raw", "source: local", f"source_date: {date}", "projects: []", "tags: [raw]", "---", ""]
    mark = [PRIVATE_HEADER, ""] if meta.private else []
    lines = [
        *front,
        f"# {title} — {meta.started_at.strftime('%Y-%m-%d %H:%M')}", "",
        *mark,
        f"Локальная запись ({app}). Участники: {who}. "
        + (f"Участники по данным Толка: {', '.join(talk_participants)}. " if talk_participants else "")
        + f"Говорящих на канале собеседников: {n_speakers}. "
        + (f"Опознаны по голосу: {', '.join(identified)}. " if identified else "")
        + (f"На микрофоне кроме владельца: {', '.join(mic_guests)}. " if mic_guests else "")
        + f"Статус: {status_text}. "
        f"«Me» — {owner} (микрофон), «Speaker N» — собеседники (системный звук)"
        + (" или гости на микрофоне" if mic_guests else "") + ", метки по голосу.", "",
        "## Полный транскрипт", "",
    ]
    next_marker = marker_every_s
    pending_breaks = sorted(breaks or [])
    for r in sorted(records, key=lambda r: r.start):
        while r.start >= next_marker:
            lines.append(f"— {hhmm(next_marker)} —")
            lines.append("")
            next_marker += marker_every_s
        while pending_breaks and pending_breaks[0][1] <= r.start:
            a, b = pending_breaks.pop(0)
            lines.append(f"— запись прервана {hhmm(a)}, продолжение {hhmm(b)} —")
            lines.append("")
        text = r.text if r.text is not None else f"[не распознано {mmss(r.start)}–{mmss(r.end)}]"
        if not text:
            continue
        lines.append(f"{r.speaker}: {text}")
        lines.append("")
    return "\n".join(lines)


def participants_of(meta: Meta, extra: list[str] | None = None, owner: str = "Owner") -> list[str]:
    """Участники заметки: владелец первым, дальше Толк из ``meta`` и плитки встречи без повторов."""
    out = [owner]
    for name in list(meta.participants) + list(extra or []):
        name = (name or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def render_stub(meta: Meta, raw_stem: str, owner: str = "Owner", thoughts_heading: str = THOUGHTS) -> str:
    date, hhmm = stamp(meta)
    title = clean_title(title_of(meta))
    participants = ", ".join(participants_of(meta, owner=owner))
    return "\n".join([
        "---", "type: meeting", f"id: MTG-{date}-{hhmm}", f"title: {title}", f"date: {date}",
        f"participants: [{participants}]", "projects: []", f'transcript: "[[{raw_stem}]]"', "tags: [meeting, stub]", "---", "",
        f"# {title}", "",
        f"**Дата:** {date} {meta.started_at.strftime('%H:%M')} · **Участники:** {participants}", "",
        thoughts_heading, "", "", "<!-- дистиллят: ожидает дейли-рутин -->", "",
    ])


def render_private_note(meta: Meta) -> str:
    """Заметки к приватной записи — обычный файл: ни фронт-маттера, ни ссылок в вольт."""
    return f"# Заметки — {meta.started_at.strftime('%Y-%m-%d %H:%M')}\n\n"


def has_stub_tag(text: str, thoughts_heading: str = THOUGHTS) -> bool:
    """Заглушка всё ещё за воркером: тег ``stub`` на месте. Мысли под ним могут быть уже написаны."""
    head, sep, _body = text.partition(thoughts_heading)
    return bool(sep) and bool(STUB_TAG.search(head))


def is_untouched_stub(text: str, thoughts_heading: str = THOUGHTS) -> bool:
    """Заглушка, в которую ещё не писали: тег ``stub`` на месте, секция мыслей пуста.

    Всё спорное считаем тронутым: снести чужую мысль хуже, чем оставить лишний файл.
    """
    if not has_stub_tag(text, thoughts_heading):
        return False
    body = text.partition(thoughts_heading)[2]
    return not [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("<!--")]


def private_dir(cfg: Config, meta: Meta) -> Path:
    return cfg.private_dir / meta.id


def raw_path(cfg: Config, meta: Meta) -> Path:
    if meta.private:
        return private_dir(cfg, meta) / "transcript.md"
    return cfg.vault_root / cfg.raw_subdir / raw_filename(meta)


def note_path(cfg: Config, meta: Meta) -> Path:
    if meta.private:
        return private_dir(cfg, meta) / "notes.md"
    return cfg.vault_root / cfg.meetings_subdir / note_filename(meta)


def write_text(cfg: Config, meta: Meta, path: Path, text: str) -> None:
    """Запись файла записи. Приватная — в каталог 0700 файлом 0600, и не в вольте.

    ``mkdir(mode=…)`` уважает umask и не трогает уже существующий каталог, поэтому права
    выставляются ``chmod`` после создания — и на корень приватного каталога тоже.
    """
    if not meta.private:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
        return
    for d in (cfg.private_dir, path.parent):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, PRIVATE_DIR_MODE)
    atomic_write_text(path, text)
    os.chmod(path, PRIVATE_FILE_MODE)


def pinned_raw_path(cfg: Config, session: Session) -> Path:
    """Путь фиксируется первой записью: смена meta.title на ходу не должна плодить второй файл."""
    pinned = session.state.raw_path
    if pinned and Path(pinned).exists():
        return Path(pinned)
    return raw_path(cfg, session.meta)


def write_raw(cfg: Config, session: Session, status_text: str,
              talk_participants: list[str] | None = None) -> Path:
    st = session.state
    p = pinned_raw_path(cfg, session)
    if st.continues or st.continued_by:
        text = render_meeting(cfg, session, status_text, talk_participants)
    else:
        sys_speakers = {r.speaker for r in st.records if r.channel == "sys"}
        n_speakers = max(len(set(st.speaker_of_emb)), len(sys_speakers))
        identified = [st.speaker_names[k] for k in sorted(st.speaker_names, key=lambda x: (len(x), x))]
        # гости на микрофоне — по первому появлению; пока профиль владельца не созрел, все Me и список пуст
        mic_guests = list(dict.fromkeys(r.speaker for r in sorted(st.records, key=lambda r: r.start)
                                        if r.channel == "mic" and r.speaker != "Me"))
        text = render_raw(session.meta, st.records, n_speakers, status_text, cfg.marker_every_s, identified,
                          talk_participants, cfg.owner_name, mic_guests)
    write_text(cfg, session.meta, p, text)
    st.raw_path = str(p)
    return p


def render_meeting(cfg: Config, session: Session, status_text: str,
                   talk_participants: list[str] | None = None) -> str:
    """Raw встречи из нескольких частей (continuation.py): шапка первой части, реплики всех со сдвигом
    времени, отметки разрывов; списки опознанных, участников Толка и гостей микрофона — объединение."""
    from .continuation import chain, merged_records
    from .talk import TalkFile

    parts = chain(cfg, session)
    records, breaks = merged_records(parts)
    n_speakers = len({r.speaker for r in records if r.channel == "sys"})
    identified = list(dict.fromkeys(name for part in parts for name in part.state.speaker_names.values()))
    talk: list[str] = []
    for part in parts:
        names = talk_participants if part is session else TalkFile(part.dir / "talk.jsonl").participants(cfg.talk_bot_patterns)
        talk += [n for n in (names or []) if n not in talk]
    mic_guests = list(dict.fromkeys(r.speaker for r in sorted(records, key=lambda r: r.start)
                                    if r.channel == "mic" and r.speaker != "Me"))
    status = f"{status_text}; частей записи: {len(parts)}"
    return render_raw(parts[0].meta, records, n_speakers, status, cfg.marker_every_s, identified,
                      talk or None, cfg.owner_name, mic_guests, breaks=breaks)


def update_stub_participants(cfg: Config, session: Session, talk_participants: list[str]) -> bool:
    """Дописать в MTG-заглушку участников, которых показал Толк. True — файл изменился.

    Правим только пока заметка за воркером, то есть пока на ней тег ``stub``: мысли под
    заголовком к этому моменту уже могли появиться, а шапка всё ещё наша. Тег сняли —
    заметку ведёт человек или дистиллер, и список участников трогать нельзя.
    """
    if session.meta.private or not talk_participants:
        return False
    pinned = session.state.note_path
    p = Path(pinned) if pinned else None
    if p is None or not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    if not has_stub_tag(text, cfg.owner_thoughts_heading):
        return False
    names = ", ".join(participants_of(session.meta, talk_participants, cfg.owner_name))
    new = FRONT_PARTICIPANTS.sub(lambda m: f"participants: [{names}]", text, count=1)
    new = BODY_PARTICIPANTS.sub(lambda m: m.group(1) + names, new, count=1)
    if new == text:
        return False
    atomic_write_text(p, new)
    return True


def ensure_stub(cfg: Config, session: Session) -> Path:
    """Заметка рядом с транскриптом: MTG-заглушка в вольте, у приватной записи — простой notes.md."""
    meta = session.meta
    pinned = session.state.note_path
    p = Path(pinned) if pinned and Path(pinned).exists() else note_path(cfg, meta)
    if not p.exists():
        text = render_private_note(meta) if meta.private else \
            render_stub(meta, pinned_raw_path(cfg, session).stem, cfg.owner_name, cfg.owner_thoughts_heading)
        write_text(cfg, meta, p, text)
    session.state.note_path = str(p)
    return p
