"""Вопросы владельца своему агенту во время встречи — отдельный файл рядом с Raw.

Приложение пишет каждый вопрос с ответом строкой JSON в ``qa.jsonl`` сессии (тот же файловый
протокол, что ``talk.jsonl``). На финализации воркер собирает их в ``Agent-<дата>-<время>-<тема>.md``
в папке Raw — только если вопросы были и запись не приватная. Префикс не ``Local-``: рутины вольта
ищут транскрипты по нему, а ответ агента — пересказ, не источник фактов встречи.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .config import Config
from .session import Meta, Session
from .vault import TITLE_CAP, clean_title, pinned_raw_path, note_filename, slug, stamp, title_of, write_text

log = logging.getLogger(__name__)

QA_FILE = "qa.jsonl"
PREFIX = "Agent-"


def qa_filename(meta: Meta) -> str:
    date, hhmm = stamp(meta)
    return f"{PREFIX}{date}-{hhmm}-{slug(title_of(meta))[:TITLE_CAP]}.md"


def qa_path_for_raw(raw: Path) -> Path:
    """Пара к закреплённому Raw: смена темы встречи не плодит второй файл вопросов."""
    stem = raw.stem.removeprefix("Local-")
    return raw.parent / f"{PREFIX}{stem}.md"


def read_qa(session_dir: Path) -> list[dict]:
    """Ответившие вопросы сессии по порядку; битая строка (оборванная запись) пропускается."""
    path = session_dir / QA_FILE
    if not path.exists():
        return []
    items = []
    # только \n: ответы агента бывают с U+2028/U+0085, а splitlines() режет строку JSON по ним
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and str(item.get("question") or "").strip() and str(item.get("answer") or "").strip():
            items.append(item)
    return items


def _clock(item: dict) -> str:
    try:
        return datetime.fromisoformat(str(item.get("asked_at"))).strftime("%H:%M")
    except ValueError:
        return "--:--"


def render_qa(meta: Meta, raw_stem: str, note_stem: str, items: list[dict]) -> str:
    date, _ = stamp(meta)
    title = clean_title(title_of(meta))
    lines = [
        "---", "type: raw", "source: other", f"source_date: {date}", "projects: []", "tags: [raw, agent-qa]", "---", "",
        f"# Вопросы агенту — {title} — {meta.started_at.strftime('%Y-%m-%d %H:%M')}", "",
        f"К встрече [[{note_stem}]], транскрипт [[{raw_stem}]]. Ответы агента — пересказ по ходу встречи, "
        "не источник фактов: факты встречи берутся только из транскрипта.", "",
    ]
    for item in items:
        question = " ".join(str(item["question"]).split())
        context = "с контекстом проектов" if item.get("with_context") else "только встреча"
        agent = str(item.get("agent") or "агент").strip()
        seconds = item.get("seconds")
        meta_line = " · ".join(p for p in [agent, context, f"{seconds} с" if seconds is not None else ""] if p)
        lines += [f"## {_clock(item)} · {question}", "", str(item["answer"]).strip(), "", f"_{meta_line}_", ""]
    return "\n".join(lines)


def write_agent_qa(cfg: Config, session: Session) -> Path | None:
    """Файл вопросов к встрече. Части одной встречи (продолжения) — в одном файле первой части."""
    if session.meta.private:
        return None
    from .continuation import chain

    parts = chain(cfg, session) if (session.state.continues or session.state.continued_by) else [session]
    items = [item for part in parts for item in read_qa(part.dir)]
    if not items:
        return None
    first = parts[0].meta
    raw = pinned_raw_path(cfg, session)
    note = Path(session.state.note_path).stem if session.state.note_path else Path(note_filename(first)).stem
    path = qa_path_for_raw(raw)
    write_text(cfg, session.meta, path, render_qa(first, raw.stem, note, items))
    log.info("%s: вопросов агенту %d → %s", session.meta.id, len(items), path.name)
    return path


def write_agent_qa_safely(cfg: Config, session: Session) -> Path | None:
    """Файл вопросов — удобство: его сбой не должен стоить встрече финализации и удаления аудио."""
    try:
        return write_agent_qa(cfg, session)
    except Exception:  # noqa: BLE001
        log.exception("%s: файл вопросов агенту не записан", session.meta.id)
        return None
