"""Судья контекста: модель сравнивает два транскрипта одного отрезка встречи.

Слова и имена оба транскрибатора пишут по-разному, и ни один не эталон. Что важно владельцу —
поймёт ли читатель, не бывший на встрече, что обсуждали и к чему пришли. Это оценивает модель
Claude (решение владельца 14.10.2026: судить моделью Anthropic): ей дают два транскрипта одного
отрезка в случайном порядке A/B, просят оценить понятность и связность каждого, назвать смысл,
потерянный в одном и сохранённый в другом, и сказать, какой лучше передаёт контекст.

Ключ API — `ANTHROPIC_API_KEY` в окружении или в `.env` каталога данных (его читает `load_config`);
в репо ключ не живёт. Без ключа судья не запускается.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Callable

from .config import Config

log = logging.getLogger("transcriber")

PROMPT = """Ты оцениваешь качество двух транскриптов ОДНОГО И ТОГО ЖЕ отрезка рабочей встречи, сделанных
двумя разными системами распознавания речи. Имена говорящих и точность отдельных слов сами по себе
не важны. Важно одно: насколько читатель, которого не было на встрече, поймёт по транскрипту, что
обсуждали, какие аргументы звучали и к чему пришли.

Оцени каждый транскрипт:
- understanding — 0..10: насколько ясно из текста, что обсуждали и к чему пришли;
- coherence — 0..10: насколько текст связный, без обрывков и бессмыслицы;
- garbled — доля 0..1 реплик, смысл которых не восстановить (мусор, обрывки, бессвязные слова).

Затем перечисли содержательные пункты (тезисы, решения, цифры, договорённости), которые есть в одном
транскрипте и отсутствуют или неузнаваемы в другом, и назови, какой транскрипт лучше передаёт контекст.
Оценивай строго и одинаково для обоих; одинаково плохие или одинаково хорошие — "equal".

Ответь ТОЛЬКО одним JSON-объектом без пояснений вокруг:
{"a": {"understanding": 0, "coherence": 0, "garbled": 0.0},
 "b": {"understanding": 0, "coherence": 0, "garbled": 0.0},
 "better": "a" | "b" | "equal",
 "missing_in_a": ["…"], "missing_in_b": ["…"],
 "comment": "одна-две фразы, почему"}

=== Транскрипт A ===
{A}

=== Транскрипт B ===
{B}
"""

Client = Callable[[str], str]      # промпт → текст ответа модели


@dataclass
class Verdict:
    ours: dict          # understanding, coherence, garbled — для нашего транскрипта
    theirs: dict        # то же для бота
    better: str         # "ours" | "theirs" | "equal"
    missing_in_ours: list[str]
    missing_in_theirs: list[str]
    comment: str
    order: str          # "ours_first" | "theirs_first" — как показывали модели
    usage: dict = field(default_factory=dict)


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def anthropic_client(cfg: Config) -> Client:
    """Claude через официальный SDK; ключ берётся из окружения (``.env`` каталога данных уже загружен)."""
    import anthropic

    client = anthropic.Anthropic(timeout=float(cfg.judge_timeout_s), max_retries=3)
    last_usage: dict = {}

    def call(prompt: str) -> str:
        response = client.messages.create(
            model=cfg.judge_model, max_tokens=4000,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("модель отказалась оценивать отрезок")
        last_usage.clear()
        last_usage.update({"input": response.usage.input_tokens, "output": response.usage.output_tokens})
        return "".join(block.text for block in response.content if block.type == "text")
    call.last_usage = last_usage   # type: ignore[attr-defined]
    return call


def parse_verdict(text: str, order: str) -> Verdict | None:
    """JSON из ответа (вокруг может быть текст); A/B переводятся в «наш/их» по порядку показа."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        a, b = _scores(d["a"]), _scores(d["b"])
        better = str(d.get("better", "equal")).strip().lower()
        miss_a, miss_b = _items(d.get("missing_in_a")), _items(d.get("missing_in_b"))
        comment = str(d.get("comment", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        return None
    ours_is_a = order == "ours_first"
    ours, theirs = (a, b) if ours_is_a else (b, a)
    if better in ("a", "b"):
        better = "ours" if (better == "a") == ours_is_a else "theirs"
    else:
        better = "equal"
    return Verdict(ours, theirs, better, miss_a if ours_is_a else miss_b, miss_b if ours_is_a else miss_a,
                   comment, order)


def _items(value) -> list[str]:
    """Список пунктов; строка — один пункт, число или None — пусто (модель иногда отвечает не списком)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return []


def _score(value, top: float) -> float:
    x = float(0 if value is None else value)
    if x != x:                       # NaN
        raise ValueError("nan")
    return min(top, max(0.0, x))


def _scores(d: dict) -> dict:
    return {"understanding": _score(d["understanding"], 10), "coherence": _score(d["coherence"], 10),
            "garbled": _score(d.get("garbled", 0.0), 1)}


def judge_chunk(client: Client, ours: str, theirs: str, rnd: random.Random | None = None) -> Verdict | None:
    """Один отрезок: случайный порядок A/B, запрос, разбор; None — сбой или ответ не по форме."""
    rnd = rnd or random.Random()
    order = "ours_first" if rnd.random() < 0.5 else "theirs_first"
    a, b = (ours, theirs) if order == "ours_first" else (theirs, ours)
    prompt = PROMPT.replace("{A}", a).replace("{B}", b)
    try:
        text = client(prompt)
    except Exception as e:  # noqa: BLE001 — сеть и модель чужие; один сбой не роняет прогон
        log.warning("judge: запрос не удался (%s)", e)
        return None
    v = parse_verdict(text, order)
    if v is None:
        log.warning("judge: ответ не разобран: %s", text[:200].replace("\n", " "))
    else:
        v.usage = dict(getattr(client, "last_usage", {}) or {})
    return v


def summarize(verdicts: list[Verdict]) -> dict:
    """Средние оценки и счёт побед по отрезкам."""
    if not verdicts:
        return {"chunks": 0}

    def mean(key, side):
        return round(sum(getattr(v, side)[key] for v in verdicts) / len(verdicts), 2)
    keys = ("understanding", "coherence", "garbled")
    return {"chunks": len(verdicts),
            "ours": {k: mean(k, "ours") for k in keys}, "theirs": {k: mean(k, "theirs") for k in keys},
            "wins_ours": sum(v.better == "ours" for v in verdicts),
            "wins_theirs": sum(v.better == "theirs" for v in verdicts),
            "equal": sum(v.better == "equal" for v in verdicts),
            "tokens_in": sum(v.usage.get("input", 0) for v in verdicts),
            "tokens_out": sum(v.usage.get("output", 0) for v in verdicts)}
