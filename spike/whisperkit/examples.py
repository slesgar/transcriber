#!/usr/bin/env python3
"""Десять реплик эталона Granola и то, что на тех же секундах выдали движки.

  ./examples.py --ref <granola.txt> label=run.json [label=run.json ...]

Привязка по времени: у эталона времён нет, поэтому опорные точки берём по первой
реплике S: (якорь «отдельную для CDP витрину») и дальше идём подряд, показывая
для каждой реплики эталона перекрывающие её по тексту сегменты движка.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re

WORD = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ+]+")


def toks(t: str) -> list[str]:
    return [w.lower() for w in WORD.findall(t)]


def best_window(hay: list[str], needle: list[str]) -> tuple[int, int, float]:
    """Окно в hay, лучше всего совпадающее с needle."""
    if not needle or not hay:
        return 0, 0, 0.0
    best = (0, 0, 0.0)
    n = len(needle)
    step = max(1, n // 4)
    for i in range(0, max(1, len(hay) - n // 2), step):
        win = hay[i:i + int(n * 1.6) + 5]
        r = difflib.SequenceMatcher(None, win, needle, autojunk=False).ratio()
        if r > best[2]:
            best = (i, i + len(win), r)
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()

    with open(a.ref) as f:
        ref_lines = [ln[2:].strip() for ln in f if ln.startswith("S:")]
    ref_lines = [ln for ln in ref_lines if len(toks(ln)) >= 4][:a.n]

    engines = {}
    for spec in a.runs:
        label, path = spec.split("=", 1)
        d = json.load(open(path))
        engines[label] = d["segments"]

    for k, ref in enumerate(ref_lines, 1):
        print(f"\n{'=' * 78}\n[{k}] Granola: {ref}")
        rt = toks(ref)
        for label, segs in engines.items():
            words, owner = [], []
            for s in segs:
                for w in toks(s["text"]):
                    words.append(w)
                    owner.append(s)
            i, j, score = best_window(words, rt)
            if score < 0.25:
                print(f"    {label:<14}: — (не найдено, лучшее совпадение {score:.2f})")
                continue
            chosen, seen = [], set()
            for s in owner[i:j]:
                key = (s["start"], s["text"])
                if key not in seen:
                    seen.add(key)
                    chosen.append(s)
            text = " ".join(s["text"] for s in chosen)
            print(f"    {label:<14}: [{chosen[0]['start']:.0f}s sim={score:.2f}] {text[:420]}")


if __name__ == "__main__":
    main()
