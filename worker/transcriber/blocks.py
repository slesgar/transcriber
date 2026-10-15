"""Склейка реплик одного канала в блок под whisper.

whisper рассчитан на окно ~30 с и сам тянет контекст внутри окна: на коротком обрывке
в 1-3 с он додумывает или молчит. Сравнение с Granola на живой встрече показало провалы
в 25-76 слов ровно там, где реплики были короткими. Поэтому реплики VAD не идут в модель
поштучно: подряд идущие реплики канала склеиваются в блок на 45-90 с, реальные паузы между
ними режутся до ``gap_keep_s`` (модель не должна тратить окно на тишину), а таблица
смещений возвращает времена сегментов обратно в оригинальную дорожку.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .vad import Utterance

# (смещение в склейке, оригинальное начало, длина) — всё в сэмплах
Piece = tuple[int, int, int]


@dataclass
class Block:
    start: int                                  # оригинальное начало первой реплики, сэмплы
    end: int                                    # оригинальный конец последней, сэмплы
    audio: np.ndarray                           # склейка, float32
    map_to_original: Callable[[float], float]   # секунды в склейке → секунды в дорожке


def to_original(pieces: list[Piece], sample_rate: int, t: float) -> float:
    """Точка склейки → точка дорожки. Внутри вырезанной паузы — конец предыдущей реплики."""
    c = t * sample_rate
    prev_end = float(pieces[0][1])
    for concat_start, orig_start, n in pieces:
        if c < concat_start:
            return prev_end / sample_rate
        if c < concat_start + n:
            return (orig_start + (c - concat_start)) / sample_rate
        prev_end = float(orig_start + n)
    return prev_end / sample_rate


class BlockBuilder:
    """Копит реплики канала и отдаёт блок, когда его пора отправлять в ASR.

    Блок закрывается, когда: набран ``min_s`` и перед следующей репликой пауза ``gap_s``;
    либо на границе реплики набрано ``max_s``; либо ``idle()`` видит, что речи нет уже
    ``idle_s`` (граница задержки для редкого диалога); либо ``flush()`` на финализации.
    """

    def __init__(self, sample_rate: int = 16000, min_s: float = 45.0, max_s: float = 90.0,
                 gap_s: float = 1.5, gap_keep_s: float = 0.5, idle_s: float = 4.0):
        self.sr = sample_rate
        self.min_len = int(sample_rate * min_s)
        self.max_len = int(sample_rate * max_s)
        self.gap = int(sample_rate * gap_s)
        self.keep = int(sample_rate * gap_keep_s)
        self.idle_len = int(sample_rate * idle_s)
        self.parts: list[np.ndarray] = []
        self.pieces: list[Piece] = []
        self.filled = 0                     # длина склейки, сэмплы
        self.last_end = 0                   # оригинальный конец последней реплики

    @property
    def open_start(self) -> int | None:
        """Оригинальное начало открытого блока — точка, с которой безопасно продолжить после падения."""
        return self.pieces[0][1] if self.pieces else None

    @property
    def span(self) -> int:
        return self.last_end - self.pieces[0][1] if self.pieces else 0

    def add(self, u: Utterance) -> list[Block]:
        out: list[Block] = []
        if self.pieces and self.span >= self.min_len and u.start - self.last_end >= self.gap:
            out.append(self._close())
        if self.pieces:
            pause = min(max(0, int(u.start) - self.last_end), self.keep)
            if pause:
                self.parts.append(np.zeros(pause, dtype=np.float32))
                self.filled += pause
        audio = np.asarray(u.audio, dtype=np.float32)
        self.pieces.append((self.filled, int(u.start), len(audio)))
        self.parts.append(audio)
        self.filled += len(audio)
        self.last_end = int(u.start) + len(audio)
        if self.span >= self.max_len:
            out.append(self._close())
        return out

    def idle(self, now_samples: int) -> list[Block]:
        """Речи нет уже ``idle_s`` — блок можно не ждать дальше."""
        if self.pieces and int(now_samples) - self.last_end >= self.idle_len:
            return [self._close()]
        return []

    def flush(self) -> list[Block]:
        return [self._close()] if self.pieces else []

    def _close(self) -> Block:
        pieces, audio = list(self.pieces), np.concatenate(self.parts)
        start, end = pieces[0][1], self.last_end
        self.parts, self.pieces, self.filled, self.last_end = [], [], 0, 0
        return Block(start=start, end=end, audio=audio,
                     map_to_original=lambda t, p=pieces, sr=self.sr: to_original(p, sr, t))
