"""Имена говорящих из интерфейса Толка.

Голос сам по себе говорит только «это один и тот же человек»; кто он — знает интерфейс встречи.
Приложение снимает плитки участников из Accessibility-дерева Толка и пишет их в ``talk.jsonl``
рядом с дорожками сессии: по объекту на выборку, при каждом изменении состояния плюс
контрольный снимок не реже чем раз в 30 секунд.

    {"t": 12.3, "participants": [{"name": "Пётр Кузьмин", "mic": false, "active": false}, …]}

``t`` — секунды от старта сессии, по тем же часам, что и времена реплик. Состояние выборки
держится до следующей: между ними Толк ничего не менял. Файла может не быть вовсе (Zoom,
приложение без дерева, приватная запись), и он может оборваться посреди встречи — тогда имён
просто нет, а транскрипт остаётся прежним.

Две породы плиток именами не распоряжаются: **переговорка** («Переговорная 5») — это один
микрофон на несколько человек, и **бот** («KTalk Secretary») — он не говорит вовсе. Их отрезки
остаются за голосовыми кластерами: подписать чужую реплику именем переговорки хуже, чем
оставить «Speaker N». В список участников встречи переговорка при этом попадает — за ней
действительно сидят люди.

Файл — недоверенные данные: из него берутся только строки и флаги, ничего не исполняется.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("transcriber")

# Те же значения лежат в Config/config.yaml — здесь они дефолт для чистых функций и тестов.
ROOM_PATTERNS = ["^\\d{3}", "переговор", "room"]
BOT_PATTERNS = ["ktalk secretary", "саммаризатор", "bot"]

MIN_SHARE = 0.6      # столько отрезка реплики должна накрывать плитка, чтобы дать ей имя

Span = tuple[float, float, str]      # (от, до, имя) — «до» последнего отрезка равно бесконечности


@dataclass
class Tile:
    """Плитка участника: имя, свой ли это микрофон, подсвечена ли говорящим прямо сейчас."""

    name: str
    mic: bool = False
    active: bool = False


@dataclass
class TalkSample:
    t: float
    participants: list[Tile] = field(default_factory=list)


# ---- чтение файла ------------------------------------------------------------


def _tiles(raw) -> list[Tile]:
    out: list[Tile] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = clean_name(str(item.get("name") or ""))
        if name:
            out.append(Tile(name, bool(item.get("mic")), bool(item.get("active"))))
    return out


# статусы соединения и качества, которые Толк дописывает прямо в подпись плитки (тикет 11: «Передача видео
# в высоком качестве» успела стать именем профиля в библиотеке голосов)
STATUS_SUFFIXES = ("Связь потеряна", "Пытаемся восстановить", "Подключается", "Reconnecting",
                   "Передача видео", "Проверяем скорость", "Видео скоро появится", "Плохое соединение",
                   "Низкое качество", "Микрофон выключен", "Демонстрация экрана")


def clean_name(name: str) -> str:
    """Толк дописывает статус соединения прямо в подпись плитки — срезаем его."""
    out = name
    for marker in STATUS_SUFFIXES:
        idx = out.find(marker)
        if idx > 0:
            out = out[:idx]
    return " ".join(out.split())


def load_talk(path: Path | str) -> list[TalkSample]:
    """Выборки из ``talk.jsonl``, отсортированные по времени. Нет файла — пустой список.

    Файл пишется на ходу, поэтому последняя строка бывает оборвана, а ключи — незнакомы:
    битая строка пропускается молча, остальные живут. Сломать транскрипт талк-файл не может.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[TalkSample] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue                       # оборванный хвост: файл пишут прямо сейчас
        if not isinstance(d, dict):
            continue
        try:
            t = float(d["t"])
        except (KeyError, TypeError, ValueError):
            continue                       # выборка без времени никуда не ложится
        if not math.isfinite(t):
            continue
        out.append(TalkSample(t, _tiles(d.get("participants"))))
    out.sort(key=lambda s: s.t)
    return out


# ---- переговорки и боты ------------------------------------------------------


def _matches(name: str, patterns: list[str]) -> bool:
    for p in patterns:
        try:
            if re.search(p, name, re.IGNORECASE):
                return True
        except re.error as e:              # кривой шаблон в конфиге не должен ронять встречу
            log.warning("talk: шаблон %r пропущен (%s)", p, e)
    return False


def is_room(name: str, patterns: list[str] | None = None) -> bool:
    """Плитка переговорки: один микрофон на несколько человек — имени из неё не выйдет."""
    return _matches(name, ROOM_PATTERNS if patterns is None else patterns)


def is_shared(name: str, names: list[str] | None) -> bool:
    """Общий микрофон по имени плитки (несколько голосов за одним человеком) или сам владелец."""
    n = name.casefold()
    return any(x.casefold() in n for x in (names or []) if x.strip())


def is_bot(name: str, patterns: list[str] | None = None) -> bool:
    """Плитка бота-саммаризатора: в разговоре не участвует, в списке участников не нужен."""
    return _matches(name, BOT_PATTERNS if patterns is None else patterns)


# ---- лента активности --------------------------------------------------------


def active_timeline(samples: list[TalkSample]) -> list[Span]:
    """Кто подсвечен говорящим и когда. Состояние держится до следующей выборки.

    Подсвечены несколько — берём первую плитку: Толк подсвечивает по громкости, и первая
    в дереве оказывается той, на ком фокус. Никто не подсвечен — в ленте просто дырка.
    Соседние отрезки одного имени склеиваются, последний тянется до конца встречи.
    """
    out: list[Span] = []
    for i, s in enumerate(samples):
        name = next((t.name for t in s.participants if t.active), None)
        if name is None:
            continue
        end = samples[i + 1].t if i + 1 < len(samples) else math.inf
        if end <= s.t:
            continue
        if out and out[-1][2] == name and out[-1][1] >= s.t:
            out[-1] = (out[-1][0], end, name)
        else:
            out.append((s.t, end, name))
    return out


def _cover(timeline: list[Span], start: float, end: float) -> dict[str, float]:
    cover: dict[str, float] = {}
    for a, b, name in timeline:
        d = min(b, end) - max(a, start)
        if d > 0:
            cover[name] = cover.get(name, 0.0) + d
    return cover


def span_share(timeline: list[Span], start: float, end: float, name: str) -> float:
    """Какую долю отрезка ``[start, end]`` накрыла плитка ``name`` — от 0 до 1."""
    total = end - start
    if total <= 0:
        return 0.0
    return min(1.0, _cover(timeline, start, end).get(name, 0.0) / total)


def speaker_for_span(timeline: list[Span], start: float, end: float, min_share: float = MIN_SHARE,
                     room_patterns: list[str] | None = None,
                     bot_patterns: list[str] | None = None,
                     shared_names: list[str] | None = None) -> str | None:
    """Имя, накрывшее отрезок не меньше чем на ``min_share``, иначе None.

    Переговорка и бот имени не дают: такой отрезок остаётся за голосовым кластером.
    """
    total = end - start
    if total <= 0 or not timeline:
        return None
    cover = _cover(timeline, start, end)
    if not cover:
        return None
    # Переговорки, боты и общие микрофоны пропускаем и смотрим на следующего по покрытию:
    # когда активны двое, первым в ленте может стоять именно общий микрофон.
    for name, covered in sorted(cover.items(), key=lambda kv: (-kv[1], kv[0])):
        if is_room(name, room_patterns) or is_bot(name, bot_patterns) or is_shared(name, shared_names):
            continue
        return name if covered / total >= min_share else None
    return None


def talk_participants(samples: list[TalkSample], bot_patterns: list[str] | None = None) -> list[str]:
    """Все, кого Толк показал за встречу, в порядке появления. Боты — вон, переговорки — как есть."""
    out: list[str] = []
    seen: set[str] = set()
    for s in samples:
        for tile in s.participants:
            if tile.name in seen or is_bot(tile.name, bot_patterns):
                continue
            seen.add(tile.name)
            out.append(tile.name)
    return out


# ---- файл сессии с ленивым перечитыванием ------------------------------------


_UNSET = object()


class TalkFile:
    """``talk.jsonl`` сессии: файл растёт по ходу встречи, поэтому перечитывается по метке.

    Метка — время изменения и размер: один ``stat`` на шаг конвейера вместо разбора файла.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._stamp: object = _UNSET
        self.samples: list[TalkSample] = []
        self.timeline: list[Span] = []
        self.refresh()

    def _mark(self):
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def refresh(self) -> bool:
        """Перечитать, если файл изменился. True — лента обновилась."""
        mark = self._mark()
        if mark == self._stamp:
            return False
        self._stamp = mark
        self.samples = load_talk(self.path) if mark is not None else []
        self.timeline = active_timeline(self.samples)
        return True

    def participants(self, bot_patterns: list[str] | None = None) -> list[str]:
        return talk_participants(self.samples, bot_patterns)
