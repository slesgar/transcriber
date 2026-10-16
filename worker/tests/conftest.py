from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from transcriber.asr import Segment
from transcriber import config as config_module
from transcriber.voices import VoiceLibrary

SR = 16000
FRAME = 160          # 10 мс — сетка, по которой FakeAsr ищет границы речи в блоке
BURST_GAP_S = 0.3    # столько тишины разделяет два всплеска речи


def tone(seconds: float, freq: float = 440.0, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float, sr: int = SR) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


def to_pcm_bytes(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()


def fake_prob(chunk: np.ndarray) -> float:
    """Speech = loud enough. Lets tests build speech/silence patterns from tones."""
    return 1.0 if float(np.abs(chunk).max()) > 0.05 else 0.0


def bursts(audio: np.ndarray, sr: int = SR, frame: int = FRAME,
           gap_s: float = BURST_GAP_S) -> list[tuple[int, int]]:
    """Границы всплесков речи в склейке — тот же порог, что у fake_prob, но по всему блоку."""
    loud = [float(np.abs(audio[i * frame:(i + 1) * frame]).max()) > 0.05 for i in range(len(audio) // frame)]
    gap = max(1, int(round(gap_s * sr / frame)))
    out: list[tuple[int, int]] = []
    start = end = None
    silent = 0
    for i, is_loud in enumerate(loud):
        if is_loud:
            start = i if start is None else start
            end = i + 1
            silent = 0
        elif start is not None:
            silent += 1
            if silent >= gap:
                out.append((start * frame, end * frame))
                start = end = None
    if start is not None:
        out.append((start * frame, end * frame))
    return out


class FakeAsr:
    """Сегмент на каждый всплеск речи в блоке; текст — по доминирующей частоте всплеска."""

    def __init__(self, fail_on: set[int] | None = None, texts: dict[int, str] | None = None):
        self.fail_on = fail_on or set()
        self.texts = texts or {}
        self.calls: list[tuple[int, str]] = []    # (частота, промпт) на каждый всплеск
        self.prompts: list[str] = []              # промпт на каждый блок

    def __call__(self, audio: np.ndarray, prompt: str = "") -> list[Segment]:
        self.prompts.append(prompt)
        out: list[Segment] = []
        for a, b in bursts(audio):
            f = dominant_freq(audio[a:b])
            self.calls.append((f, prompt))
            if f in self.fail_on:
                raise RuntimeError("asr boom")
            out.append(Segment(a / SR, b / SR, self.texts.get(f, f"tone{f}")))
        return out


def dominant_freq(audio: np.ndarray, sr: int = SR) -> int:
    spectrum = np.abs(np.fft.rfft(audio))
    return int(round(np.fft.rfftfreq(len(audio), 1 / sr)[int(spectrum.argmax())]))


class FakeEmbedder:
    """One-hot-ish vector per frequency band: same tone → identical embedding."""

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        f = dominant_freq(audio)
        v = np.zeros(8, dtype=np.float32)
        v[(f // 100) % 8] = 1.0
        return v


def make_session(sessions_dir: Path, session_id: str = "2026-09-15-1430", app: str = "kontur.talk",
                 title: str = "Тестовая встреча", participants: list[str] | None = None,
                 private: bool = False) -> Path:
    d = sessions_dir / session_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": session_id,
        "started_at": "2026-09-15T14:30:00+03:00",
        "app": app,
        "title": title,
        "participants": participants or ["Иван Сидоров"],
    }
    if private:
        meta["private"] = True
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    return d


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Raw/Transcripts").mkdir(parents=True)
    (root / "Meetings").mkdir(parents=True)
    return root


@pytest.fixture
def private_dir(tmp_path: Path) -> Path:
    """Каталог приватных записей в тестах — временный: рабочий ~/Documents трогать нельзя."""
    return tmp_path / "private"


def vault_files(vault: Path) -> list[Path]:
    """Всё, что лежит в вольте: приватная сессия не должна оставить там ни файла."""
    return [p for p in vault.rglob("*") if p.is_file()]


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Каталог данных в тестах — всегда временный: конфиги, сессии и голоса установки не читаются."""
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path)
    monkeypatch.setenv("TRANSCRIBER_DATA", str(tmp_path))


def one_hot(freq: int) -> list[float]:
    """Тот же вектор, что вернёт FakeEmbedder на тоне этой частоты — им и засеиваем библиотеку."""
    v = [0.0] * 8
    v[(freq // 100) % 8] = 1.0
    return v


def seed_voice(tmp_path: Path, name: str, vectors: list[list[float]], source: str = "старая:Speaker 1") -> None:
    VoiceLibrary(tmp_path / "voices").add(name, vectors, source=source)
