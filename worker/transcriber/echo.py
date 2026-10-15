from __future__ import annotations

import re
from difflib import SequenceMatcher

from .session import Rec


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower(), flags=re.UNICODE).split())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def overlap_fraction(m: Rec, others: list[Rec]) -> float:
    dur = m.end - m.start
    if dur <= 0:
        return 0.0
    total = sum(max(0.0, min(m.end, o.end) - max(m.start, o.start)) for o in others)
    return min(1.0, total / dur)


def is_echo(m: Rec, others: list[Rec], overlap: float, sim: float, strong: float = 0.8) -> bool:
    """A mic utterance is an echo of the speakers if it overlaps sys speech and says the same thing.

    Two rules, either of them is enough:
    * ``overlap`` of the mic utterance is covered by sys speech **and** the texts are ``sim``-close;
    * the texts are ``strong``-close and there is any time overlap at all — the mic side of an echo
      is often segmented into one long utterance around a short sys one, which sinks the coverage.
    """
    if not m.text:
        return False
    touching = [o for o in others if o.text and min(m.end, o.end) - max(m.start, o.start) > 0]
    if not touching:
        return False
    best = max(similarity(m.text, o.text) for o in touching)
    return (overlap_fraction(m, touching) >= overlap and best >= sim) or best >= strong


# Дежурные фразы, которые whisper сочиняет на обрывках задавленного эха (0.2–1 с остатка).
STOCK_PHRASES = {"спасибо", "спасибо за просмотр", "спасибо за внимание", "продолжение следует",
                 "всем пока", "пока", "до свидания", "угу", "ага", "да"}
RESIDUE_MAX_S = 2.0
RESIDUE_MARGIN_S = 2.0   # остаток эха всплывает и в паузах между фразами собеседника


def is_residue(m: Rec, others: list[Rec]) -> bool:
    """Короткая дежурная фраза на микрофоне, пока из динамиков кто-то говорит, — остаток эха.

    Та же фраза в тишине динамиков — реплика владельца и остаётся. Содержательный текст поверх
    чужой речи тоже остаётся: это настоящий разговор внахлёст.
    """
    if not m.text or m.end - m.start > RESIDUE_MAX_S:
        return False
    if normalize(m.text) not in STOCK_PHRASES:
        return False
    return any(o.text and min(m.end, o.end + RESIDUE_MARGIN_S) - max(m.start, o.start - RESIDUE_MARGIN_S) > 0
               for o in others)


def is_acoustic_residue(m: Rec, others: list[Rec], kept_max: float, min_cover: float) -> bool:
    """Реплика микрофона, от которой подавитель эха почти ничего не оставил, поверх речи динамиков.

    ``m.kept`` — доля энергии микрофона, пережившая подавление. У эха она 0.05–0.15, у владельца,
    говорящего поверх собеседника, от 0.17, в тишине динамиков — около единицы. Текст тут не
    помогает: на таком остатке whisper пишет бессмыслицу, непохожую на то, что сказал собеседник.
    Без доли (подавление выключено, старая запись) или без перекрытия — не решаем. Перекрытие
    считается по всем репликам динамиков, даже выброшенным фильтрами: звук там всё равно был.
    """
    if not m.text or m.kept is None or m.kept >= kept_max:
        return False
    return overlap_fraction(m, others) >= min_cover


REPEATED_MAX_WORDS = 2     # короче — «реплика», которую whisper сочиняет на остатке эха
REPEATED_MIN_TIMES = 5     # столько раз одна и та же короткая фраза за встречу — не речь, а шум (на 100 реальных
                           # сессиях при 3 снималась только «секунду» ×3 — живая реплика; «Простите» ×14 ловится и при 5)
# настоящие короткие реакции владельца поверх собеседника: их он и правда говорит десятки раз за встречу
BACKCHANNEL = {"да", "нет", "угу", "ага", "ок", "окей", "хорошо", "понял", "поняла", "спасибо", "конечно",
               "ясно", "супер", "отлично", "ну", "вот", "так", "ладно", "точно", "верно", "именно", "понятно",
               "принято", "согласен", "согласна", "давай", "давайте", "смотри", "слушай", "дада", "ага ага",
               "угу угу", "да да", "ну да", "нет нет", "ну вот", "ну ладно", "так да", "да конечно", "да хорошо",
               "всё верно", "все верно", "ну хорошо", "да нет", "вот именно", "ну смотри", "секунду", "минуту",
               "сейчас", "подожди", "подождите", "одну секунду", "один момент", "момент", "всё", "все", "ага да"}


def drop_repeated_residue(records: list[Rec], min_cover: float = 0.8) -> tuple[list[Rec], list[Rec]]:
    """Сессионный фильтр остатка эха: одна и та же короткая фраза микрофона (до ``REPEATED_MAX_WORDS``
    слов) поверх речи динамиков, повторённая за встречу ``REPEATED_MIN_TIMES`` раз и больше, — не речь
    владельца, а галлюцинация whisper на шипении («Простите.» ×14 на записи 12.10, тикет 13).

    Настоящие короткие реакции владельца («да», «угу», «понял») в список не попадают: их он и правда
    говорит десятки раз поверх собеседника. Фраза в тишине динамиков тоже остаётся. Возвращает
    (оставшиеся, снятые) в исходном порядке.
    """
    sys_recs = [r for r in records if r.channel == "sys"]
    candidates: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        if r.channel != "mic" or not r.text:
            continue
        key = normalize(r.text)
        if not key or len(key.split()) > REPEATED_MAX_WORDS or key in BACKCHANNEL:
            continue
        if overlap_fraction(r, sys_recs) >= min_cover:
            candidates.setdefault(key, []).append(i)
    doomed = {i for idx in candidates.values() if len(idx) >= REPEATED_MIN_TIMES for i in idx}
    kept = [r for i, r in enumerate(records) if i not in doomed]
    dropped = [r for i, r in enumerate(records) if i in doomed]
    return kept, dropped
