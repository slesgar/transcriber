#!/usr/bin/env python3
"""Сравнение выводов ASR: WhisperKit против mlx-whisper и оба против эталона Granola.

  ./compare.py out/wk-turbo.json out/mlx-turbo.json --ref <granola.txt>

(a) пословное совпадение WhisperKit vs mlx по всему файлу — difflib, autojunk=False;
(b) оба против эталона Granola на опорном участке (только реплики S: — удалённый
    собеседник, то есть ровно то, что лежит в sys.pcm), участок ищется по якорям;
(c) счётчик явных галлюцинаций: строки из одних слов промпта и повторяющиеся n-граммы.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys

WORD = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ+]+")

# те же фразы, что воркер вырезает из текста (worker/config.yaml)
HALLUCINATION_PHRASES = [
    "субтитры сделал dimatorzok",
    "субтитры создавал dimatorzok",
    "редактор субтитров а.семкин",
    "корректор а.егорова",
    "продолжение следует",
    "спасибо за просмотр",
]

PROMPT_WORDS = {
    "ромашка", "омни", "cdp", "feature", "store", "аб", "тест", "толк",
    "clickhouse", "dwh", "todoist", "гросс", "маржа", "м", "футтех",
}


def tokens(text: str) -> list[str]:
    return [w.lower() for w in WORD.findall(text)]


def is_hallucination_phrase(text: str) -> bool:
    t = " ".join(tokens(text))
    return any(t == " ".join(tokens(p)) or t.startswith(" ".join(tokens(p))) and len(t) < len(p) + 12
               for p in HALLUCINATION_PHRASES)


def is_prompt_only(text: str) -> bool:
    ws = tokens(text)
    return bool(ws) and len(ws) >= 2 and all(w in PROMPT_WORDS for w in ws)


def has_repeated_ngram(text: str, n: int = 3, times: int = 3) -> bool:
    ws = tokens(text)
    if len(ws) < n * times:
        return False
    counts: dict[tuple[str, ...], int] = {}
    for i in range(len(ws) - n + 1):
        g = tuple(ws[i:i + n])
        counts[g] = counts.get(g, 0) + 1
        if counts[g] >= times:
            return True
    return False


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def clean_segments(doc: dict) -> tuple[list[dict], dict]:
    """Сегменты без чистых галлюцинаций + их счётчик."""
    kept, stats = [], {"phrase": 0, "prompt_only": 0, "repeated_ngram": 0}
    for s in doc["segments"]:
        text = s["text"]
        if is_hallucination_phrase(text):
            stats["phrase"] += 1
            continue
        if is_prompt_only(text):
            stats["prompt_only"] += 1
            continue
        if has_repeated_ngram(text):
            stats["repeated_ngram"] += 1
            # повтор внутри живой реплики — не выбрасываем, только считаем
        kept.append(s)
    stats["total"] = stats["phrase"] + stats["prompt_only"] + stats["repeated_ngram"]
    return kept, stats


def all_tokens(segs: list[dict]) -> list[str]:
    return tokens(" ".join(s["text"] for s in segs))


def overlap(a: list[str], b: list[str]) -> tuple[int, float, float]:
    """Общие токены и доля от каждой стороны."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    return matched, (100.0 * matched / len(a) if a else 0.0), (100.0 * matched / len(b) if b else 0.0)


def find_sub(hay: list[str], needle: list[str]) -> int:
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            return i
    return -1


def ref_span_tokens(segs: list[dict], start_anchor: str, end_anchor: str) -> tuple[list[str], str]:
    """Токены ASR от стартового якоря до последнего вхождения конечного."""
    ws = all_tokens(segs)
    sa, ea = tokens(start_anchor), tokens(end_anchor)
    i = find_sub(ws, sa)
    if i < 0:
        return [], f"стартовый якорь {start_anchor!r} не найден"
    # конечный якорь — последнее вхождение после старта
    j = -1
    for k in range(i, len(ws) - len(ea) + 1):
        if ws[k:k + len(ea)] == ea:
            j = k + len(ea)
    if j < 0:
        return [], f"конечный якорь {end_anchor!r} не найден после старта"
    return ws[i:j], f"токены {i}..{j} из {len(ws)}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="label=path.json")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--start-anchor", default="отдельную для CDP витрину")
    ap.add_argument("--end-anchor", default="Яндекс")
    ap.add_argument("--examples", type=int, default=10)
    a = ap.parse_args()

    with open(a.ref) as f:
        ref_lines = [ln[2:].strip() for ln in f if ln.startswith("S:")]
    ref_all = tokens(" ".join(ref_lines))
    i = find_sub(ref_all, tokens(a.start_anchor))
    ref_span = ref_all[i:] if i >= 0 else ref_all
    print(f"эталон Granola: {len(ref_lines)} реплик S:, {len(ref_all)} слов, "
          f"опорный участок от якоря — {len(ref_span)} слов\n")

    docs = {}
    for spec in a.runs:
        label, path = spec.split("=", 1)
        doc = load(path)
        kept, stats = clean_segments(doc)
        docs[label] = {"doc": doc, "segs": kept, "hall": stats}

    print("=== объём и нагрузка ===")
    hdr = f"{'прогон':<22}{'сегм':>6}{'слов':>8}{'wall s':>9}{'xRT':>8}{'галлюц':>8}"
    print(hdr)
    for label, d in docs.items():
        ws = all_tokens(d["segs"])
        print(f"{label:<22}{len(d['segs']):>6}{len(ws):>8}"
              f"{d['doc']['wallSeconds']:>9.1f}{d['doc']['realtimeFactor']:>8.2f}"
              f"{d['hall']['total']:>8}")
        d["tokens"] = ws

    print("\n=== галлюцинации по типам ===")
    for label, d in docs.items():
        print(f"{label:<22}{d['hall']}")

    labels = list(docs)
    if len(labels) >= 2:
        print("\n=== пословное совпадение между прогонами (весь файл) ===")
        for x in range(len(labels)):
            for y in range(x + 1, len(labels)):
                la, lb = labels[x], labels[y]
                m, pa, pb = overlap(docs[la]["tokens"], docs[lb]["tokens"])
                print(f"{la} vs {lb}: общих {m} слов — {pa:.1f}% от {la}, {pb:.1f}% от {lb}")

    print("\n=== против эталона Granola на опорном участке ===")
    spans = {}
    for label, d in docs.items():
        span, note = ref_span_tokens(d["segs"], a.start_anchor, a.end_anchor)
        spans[label] = span
        if not span:
            print(f"{label:<22}{note}")
            continue
        m, p_asr, p_ref = overlap(span, ref_span)
        print(f"{label:<22}{len(span):>5} слов ({note}) — общих {m}: "
              f"{p_asr:.1f}% от ASR, {p_ref:.1f}% от эталона")

    print(f"\n=== {a.examples} примеров строк на опорном участке ===")
    for label, d in docs.items():
        span, _ = ref_span_tokens(d["segs"], a.start_anchor, a.end_anchor)
        if not span:
            continue
        start_w = tokens(a.start_anchor)
        # сегменты, попадающие в участок — по первому сегменту, содержащему стартовый якорь
        idx0 = next((k for k, s in enumerate(d["segs"])
                     if find_sub(tokens(s["text"]), start_w) >= 0), 0)
        print(f"\n--- {label} ---")
        for s in d["segs"][idx0:idx0 + a.examples]:
            print(f"[{s['start']:7.1f}] {s['text']}")
    print("\n--- эталон Granola (S:) ---")
    for ln in ref_lines[:a.examples]:
        print(f"[   ref ] {ln[:400]}")


if __name__ == "__main__":
    sys.exit(main())
