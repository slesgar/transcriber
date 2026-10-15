"""Склейка перезапусков одной встречи (тикет 07).

Запись обрывается (перезагрузка, остановка руками, карточка «тишина») и стартует заново — приложение
заводит новую сессию, воркер писал бы второй Raw и вторую заглушку на ту же встречу. Сессия с тем же
названием, начавшаяся не позже ``continue_gap_min`` минут после конца предыдущей финализированной, —
продолжение: она подхватывает её Raw и заметку, а транскрипт собирается из всех частей с отметкой
«запись прервана». Части остаются отдельными сессиями (своё аудио, свой state), связь — поля
``continues`` / ``continued_by`` в state.json. Безымянные встречи («Встреча в Толке») не склеиваем:
названия нет — нет и уверенности, что это та же встреча.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .session import Rec, Session
from .speakers import label, speaker_index

log = logging.getLogger("transcriber")

MAX_CHAIN = 20      # больше частей у одной встречи не бывает; защита от кольца в state.json


def session_end(session: Session) -> datetime:
    """Когда часть кончилась: старт плюс конец последней реплики (нет реплик — старт)."""
    last = max((r.end for r in session.state.records), default=0.0)
    return session.meta.started_at + timedelta(seconds=last)


def _load(d: Path) -> Session | None:
    """Сессия с диска или None: битый сосед не должен мешать старту новой записи."""
    if not d.is_dir() or not (d / "meta.json").exists():
        return None
    try:
        return Session.load(d)
    except Exception as e:  # noqa: BLE001 — любая порча чужого каталога здесь не наша забота
        log.warning("%s: сессия не читается (%s) — пропущена при поиске продолжения", d.name, e)
        return None


def _meta_fits(d: Path, title: str, started_at: datetime) -> bool:
    """Дешёвый отсев по meta.json до чтения state и сайдкара: название, приватность, порядок."""
    from .session import Meta
    from .vault import title_of

    try:
        meta = Meta.from_dict(json.loads((d / "meta.json").read_text()))
    except Exception:  # noqa: BLE001
        return False
    return not meta.private and title_of(meta) == title and meta.started_at < started_at


def find_previous(cfg: Config, session: Session) -> Session | None:
    """Предыдущая часть той же встречи или None.

    Кандидат: та же папка сессий, то же название (не умолчание), не приватная, финализирована,
    ещё никем не продолжена, началась раньше, а кончилась не раньше чем за ``continue_gap_min``
    минут до старта этой. Из нескольких — самая поздняя.
    """
    from .vault import default_title, title_of

    if cfg.continue_gap_min <= 0 or session.meta.private:
        return None
    title = title_of(session.meta)
    if title == default_title(session.meta):
        return None
    best: Session | None = None
    for d in sorted(cfg.sessions_dir.iterdir()):
        if d.name == session.dir.name or not d.is_dir() or not (d / "meta.json").exists():
            continue
        if not _meta_fits(d, title, session.meta.started_at):
            continue
        prev = _load(d)
        if prev is None or prev.state.status != "finalized":
            continue
        # уже продолжена другой сессией — чужая; продолжена этой же (крах между двумя записями) — наша
        if prev.state.continued_by and prev.state.continued_by != session.meta.id:
            continue
        if session.meta.started_at - session_end(prev) > timedelta(minutes=cfg.continue_gap_min):
            continue
        if best is None or prev.meta.started_at > best.meta.started_at:
            best = prev
    return best


def attach(cfg: Config, session: Session) -> Session | None:
    """Прицепить новую сессию к предыдущей части: общие Raw и заметка, обратная ссылка у предыдущей."""
    prev = find_previous(cfg, session)
    if prev is None:
        return None
    session.state.continues = prev.meta.id
    session.state.raw_path = prev.state.raw_path
    session.state.note_path = prev.state.note_path
    for kind, path in (("Raw", prev.state.raw_path), ("заметка", prev.state.note_path)):
        if path and not Path(path).exists():
            log.warning("%s: %s предыдущей части переехала (%s) — будет создана заново", session.meta.id, kind, path)
    if prev.state.continued_by != session.meta.id:
        prev.state.continued_by = session.meta.id
        prev.save_state()
    log.info("%s: продолжение %s (разрыв %.0f с), Raw и заметка общие", session.meta.id, prev.meta.id,
             (session.meta.started_at - session_end(prev)).total_seconds())
    return prev


def chain(cfg: Config, session: Session) -> list[Session]:
    """Все части встречи по порядку; текущая сессия — тем же объектом, остальные с диска."""
    parts = [session]
    cur, n = session, 0
    while cur.state.continues and n < MAX_CHAIN:
        cur = _load(cfg.sessions_dir / cur.state.continues)
        if cur is None or cur.meta.private:     # приватную часть в общий Raw не берём
            break
        parts.insert(0, cur)
        n += 1
    cur, n = session, 0
    while cur.state.continued_by and n < MAX_CHAIN:
        cur = _load(cfg.sessions_dir / cur.state.continued_by)
        if cur is None or cur.meta.private:
            break
        parts.append(cur)
        n += 1
    return parts


def merged_records(parts: list[Session]) -> tuple[list[Rec], list[tuple[float, float]]]:
    """Реплики всех частей в секундах от старта первой; «Speaker N» второй части нумеруются дальше
    первой, чтобы разные голоса не слились под одним номером. Вторая величина — разрывы (конец, старт).
    ``Rec.emb`` в копиях — индекс в массивах своей части; для общего Raw он не используется."""
    root = parts[0].meta.started_at
    out: list[Rec] = []
    breaks: list[tuple[float, float]] = []
    anon_offset = 0
    prev_end: float | None = None
    for part in parts:
        offset = (part.meta.started_at - root).total_seconds()
        if prev_end is not None:
            breaks.append((prev_end, offset))
        seen = 0
        for r in part.state.records:
            idx = speaker_index(r.speaker)
            speaker = label(idx - 1 + anon_offset) if idx else r.speaker
            seen = max(seen, idx or 0)
            out.append(replace(r, start=r.start + offset, end=r.end + offset, speaker=speaker))
        anon_offset += seen
        prev_end = offset + max((r.end for r in part.state.records), default=0.0)
    return out, breaks
