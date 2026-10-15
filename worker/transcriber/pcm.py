from __future__ import annotations

from pathlib import Path

import numpy as np

BYTES_PER_SAMPLE = 2  # int16 mono


class PcmReader:
    """Reads int16 mono PCM from a file that may still be growing, starting at a sample offset."""

    def __init__(self, path: Path, offset_samples: int = 0):
        self.path = Path(path)
        self.offset = int(offset_samples)

    def available(self) -> int:
        if not self.path.exists():
            return 0
        return max(0, self.path.stat().st_size // BYTES_PER_SAMPLE - self.offset)

    def read_all(self, max_samples: int | None = None) -> np.ndarray:
        n = self.available()
        if max_samples is not None:
            n = min(n, max(0, int(max_samples)))
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        with open(self.path, "rb") as f:
            f.seek(self.offset * BYTES_PER_SAMPLE)
            raw = f.read(n * BYTES_PER_SAMPLE)
        data = _decode(raw)
        self.offset += len(data)
        return data


def _decode(raw: bytes) -> np.ndarray:
    raw = raw[: len(raw) // BYTES_PER_SAMPLE * BYTES_PER_SAMPLE]   # the writer may be mid-sample
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def read_range(path: Path, start: int, end: int) -> np.ndarray:
    """Samples [start, end) of a PCM file. Short or missing file → whatever is there."""
    path = Path(path)
    start, end = max(0, int(start)), int(end)
    if end <= start or not path.exists():
        return np.zeros(0, dtype=np.float32)
    with open(path, "rb") as f:
        f.seek(start * BYTES_PER_SAMPLE)
        raw = f.read((end - start) * BYTES_PER_SAMPLE)
    return _decode(raw)
