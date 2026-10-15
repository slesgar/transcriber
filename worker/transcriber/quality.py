"""Guards against whisper output that is not speech.

Everything here is a pure function over the decoded text: the first real meeting showed that
whisper, fed an ``initial_prompt`` with participants and glossary, recites that prompt back on
unclear audio («Owner, Ромашка, Омни, CDP, Факт, Анна Петрова.»), loops n-grams
(«Ромашка, Офлайн, Ромашка, Офлайн…»), drops an English line into a Russian call and fills pauses
with «Аааа... Ааа...». Such an utterance is dropped from the transcript, not marked unrecognised:
there was nothing to recognise.
"""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
FILLER_RE = re.compile(r"^(а+|э+|м+|ну|угу|ага|хм+|ммм+|эм+)$")
PROMPT_KEYWORDS = ("Участники", "Участник", "Термины", "Термин", "Термина")   # модель склоняет заголовки промпта

LEAK_MIN_WORDS = 2
LEAK_SHARE = 0.6
REPEAT_TIMES = 3
REPEAT_SHARE = 0.5          # доля слов фразы, занятая повтором
VARIETY_MIN_WORDS = 6
VARIETY_SHARE = 0.35
CYRILLIC_SHARE = 0.3
FOREIGN_MIN_WORDS = 3


def tokenize(text: str) -> list[str]:
    return WORD_RE.findall((text or "").lower())


def prompt_vocabulary(participants: list[str], glossary: list[str], owner: str = "Owner") -> set[str]:
    """Every word whisper could echo back from the prompt built by ``asr.build_prompt``."""
    out: set[str] = set()
    for s in (*PROMPT_KEYWORDS, owner, *participants, *glossary):
        out.update(tokenize(s))
    return out


def prompt_leak(text: str, prompt_words: set[str]) -> bool:
    w = tokenize(text)
    if len(w) < LEAK_MIN_WORDS:
        return False
    hits = sum(1 for x in w if x in prompt_words)
    if len(w) == 2:
        return hits == 2          # «Термина, Громов» — целиком из промпта, доля тут не работает
    # короткая настоящая фраза из двух терминов («Есть Ромашка Омни») — не утечка: с длинным
    # глоссарием (тикет 09) такие снимались; утечка — это три и больше слов подряд из подсказки
    return hits >= 3 and hits / len(w) >= LEAK_SHARE


def _max_consecutive(w: list[str], n: int) -> int:
    best = 1
    for i in range(len(w) - n + 1):
        gram, k, j = w[i:i + n], 1, i + n
        while w[j:j + n] == gram:
            k += 1
            j += n
        best = max(best, k)
    return best


def repetitive(text: str) -> bool:
    w = tokenize(text)
    if not w:
        return False
    # Повтор — галлюцинация, только когда он занимает заметную часть фразы: «да, да, да» внутри
    # живой речи на 30 слов — не повод выбрасывать 25 секунд разговора.
    for n in (1, 2, 3):
        k = _max_consecutive(w, n)
        if k >= REPEAT_TIMES and k * n / len(w) >= REPEAT_SHARE:
            return True
    return len(w) >= VARIETY_MIN_WORDS and len(set(w)) / len(w) < VARIETY_SHARE


def foreign(text: str, language: str = "ru") -> bool:
    """For a Russian call: a line that is essentially not Cyrillic at all."""
    if language != "ru" or len(tokenize(text)) < FOREIGN_MIN_WORDS:
        return False
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return False
    cyrillic = sum(1 for c in alpha if "Ѐ" <= c <= "ӿ")
    return cyrillic / len(alpha) < CYRILLIC_SHARE


def filler_only(text: str) -> bool:
    w = tokenize(text)
    return bool(w) and all(FILLER_RE.match(x) for x in w)


# Ярлык промпта в начале реплики («Термина, …», «Термины.») и выдуманный «тег говорящего»
# («Терминатор Ивановича, М.М.:») — утечка формата, а не речи: срезаем приставку, речь оставляем.
_LABEL = r"(?:Термин(?:ы|а|ов|атор\w*)?|Участник(?:и|а|ов)?)"
_FAKE_TAG = re.compile(_LABEL + r"(?:\s+[А-ЯЁ][а-яё]+){0,2},?\s*(?:[А-ЯЁ]\.\s?){1,3}:\s*")
_LEAD = re.compile(r"^\s*" + _LABEL + r"\s*[,.:…]+\s*")


def strip_leak_prefix(text: str) -> str:
    out = _FAKE_TAG.sub("", text)
    prev = None
    while prev != out:
        prev = out
        out = _LEAD.sub("", out)
    return re.sub(r"\s+", " ", out).strip()


def junk_reason(text: str, prompt_words: set[str], language: str = "ru") -> str | None:
    """Name of the rule that rejects this utterance, or None if it is worth keeping."""
    if not text:
        return None
    if prompt_leak(text, prompt_words):
        return "prompt leak"
    if repetitive(text):
        return "repetitive"
    if foreign(text, language):
        return "foreign"
    if filler_only(text):
        return "filler"
    return None
