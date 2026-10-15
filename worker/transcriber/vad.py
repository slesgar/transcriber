from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

CHUNK = 512  # silero expects 512 samples at 16 kHz


@dataclass
class Utterance:
    start: int  # absolute sample index
    end: int
    audio: np.ndarray

    @property
    def samples(self) -> int:
        return self.end - self.start


class Segmenter:
    def __init__(self, speech_prob: Callable[[np.ndarray], float], sample_rate: int = 16000,
                 threshold: float = 0.5, min_silence_ms: int = 700, speech_pad_ms: int = 100,
                 max_utterance_s: float = 30.0, start_sample: int = 0,
                 preroll: np.ndarray | None = None):
        self.prob = speech_prob
        self.sr = sample_rate
        self.threshold = threshold
        self.min_silence = int(sample_rate * min_silence_ms / 1000)
        self.pad = int(sample_rate * speech_pad_ms / 1000)
        self.max_len = int(sample_rate * max_utterance_s)
        self.pos = int(start_sample)          # absolute index of the next chunk to consume
        self.remainder = np.zeros(0, dtype=np.float32)
        self.open_start: int | None = None    # absolute start of the open utterance (pad applied)
        self.silence_run = 0                  # samples of silence inside the open utterance
        self.buf: list[np.ndarray] = []       # chunks since buf_start
        self.buf_start = self.pos
        self.tail = np.zeros(0, dtype=np.float32)  # audio before speech, kept for the pad
        if preroll is not None and self.pad:
            # resume: pos stays at start_sample, but the pad before it is already known
            self.tail = np.asarray(preroll, dtype=np.float32)[-self.pad:]

    def _keep_tail(self, audio: np.ndarray) -> np.ndarray:
        # speech_pad_ms == 0 → no pad at all; audio[-0:] would keep everything
        return np.asarray(audio, dtype=np.float32)[-self.pad:] if self.pad else np.zeros(0, dtype=np.float32)

    def boundary(self) -> int:
        """Safe resume point: start of the open utterance, else the read head.

        Resegmenting from here reproduces the same utterances, but not bit-exactly: the
        chunk grid restarts at this offset, so boundaries can shift by up to one chunk.
        Pass the `pad` samples before it as `preroll`, otherwise the first utterance after
        a restart also loses its leading pad.
        """
        return self.open_start if self.open_start is not None else self.pos

    def flush(self) -> list[Utterance]:
        """Close whatever is still open — the recording stopped mid-speech."""
        if self.open_start is None:
            return []
        out: list[Utterance] = []
        if len(self.remainder):
            rest = np.concatenate([self.remainder, np.zeros(-len(self.remainder) % CHUNK, dtype=np.float32)])
            self.remainder = np.zeros(0, dtype=np.float32)
            for i in range(len(rest) // CHUNK):
                out.extend(self._consume(rest[i * CHUNK:(i + 1) * CHUNK]))
        if self.open_start is not None:
            out.append(self._close(self.pos))
        return out

    def feed(self, audio: np.ndarray) -> list[Utterance]:
        data = np.concatenate([self.remainder, np.asarray(audio, dtype=np.float32)])
        n_full = len(data) // CHUNK
        self.remainder = data[n_full * CHUNK:]
        out: list[Utterance] = []
        for i in range(n_full):
            chunk = data[i * CHUNK:(i + 1) * CHUNK]
            out.extend(self._consume(chunk))
        return out

    def _consume(self, chunk: np.ndarray) -> list[Utterance]:
        speech = self.prob(chunk) >= self.threshold
        out: list[Utterance] = []
        if self.open_start is None:
            if speech:
                pad_avail = min(self.pad, len(self.tail))
                self.open_start = self.pos - pad_avail
                self.buf_start = self.pos - len(self.tail)
                self.buf = [self.tail, chunk] if len(self.tail) else [chunk]
                self.silence_run = 0
            else:
                self.tail = self._keep_tail(np.concatenate([self.tail, chunk]))
            self.pos += CHUNK
            return out
        self.buf.append(chunk)
        self.pos += CHUNK
        if speech:
            self.silence_run = 0
        else:
            self.silence_run += CHUNK
        if self.silence_run >= self.min_silence:
            end = min(self.pos - self.silence_run + self.pad, self.pos)
            out.append(self._close(end))
            self.tail = self._keep_tail(chunk)
        elif self.pos - self.open_start >= self.max_len:
            out.append(self._close(self.pos))
            self.tail = np.zeros(0, dtype=np.float32)
        return out

    def _close(self, end: int) -> Utterance:
        full = np.concatenate(self.buf)
        start = self.open_start
        assert start is not None
        audio = full[start - self.buf_start:end - self.buf_start]
        self.open_start = None
        self.buf = []
        self.silence_run = 0
        return Utterance(start=start, end=end, audio=audio)
