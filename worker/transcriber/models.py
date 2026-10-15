from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .asr import Segment
from .config import Config


@dataclass
class Models:
    asr: Callable[[np.ndarray, str], list[Segment]]
    embed: Callable[[np.ndarray], np.ndarray]
    prob_factory: Callable[[], Callable[[np.ndarray], float]]
    release: Callable[[], None] = lambda: None   # отдать системе, что можно, пока встреч нет (кеш MLX)


def silero_prob_factory(sample_rate: int) -> Callable[[], Callable[[np.ndarray], float]]:
    import torch
    from silero_vad import load_silero_vad

    def make() -> Callable[[np.ndarray], float]:
        model = load_silero_vad()

        def prob(chunk: np.ndarray) -> float:
            # silero держит состояние между вызовами; с autograd каждый вызов дописывал граф к этому
            # состоянию — ~21 КБ на 32 мс звука, воркер рос на ~80 МБ за минуту встречи
            with torch.inference_mode():
                return float(model(torch.from_numpy(np.ascontiguousarray(chunk, dtype=np.float32)), sample_rate).item())

        return prob

    return make


def load_models(cfg: Config) -> Models:
    from .asr import Transcriber
    from .speakers import Embedder

    asr = Transcriber(cfg.whisper_model, cfg.language, cfg.hallucinations, cfg.sample_rate, cfg.mlx_cache_mb)
    return Models(
        asr=asr,
        embed=Embedder(cfg.embedding_model, cfg.sample_rate),
        prob_factory=silero_prob_factory(cfg.sample_rate),
        release=asr.release,
    )
