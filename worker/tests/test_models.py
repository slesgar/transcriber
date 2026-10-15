import sys
import types

import numpy as np


def test_silero_runs_without_autograd(monkeypatch):
    """silero хранит состояние между вызовами: с включённым autograd каждый вызов дописывает граф
    к этому состоянию, и воркер рос на ~80 МБ за минуту встречи (06.10)."""
    import torch

    from transcriber.models import silero_prob_factory

    seen = []

    def model(x, sr):
        seen.append(torch.is_inference_mode_enabled())
        return torch.tensor([0.7])

    monkeypatch.setitem(sys.modules, "silero_vad", types.SimpleNamespace(load_silero_vad=lambda: model))
    prob = silero_prob_factory(16000)()
    assert prob(np.zeros(512, dtype=np.float32)) == np.float32(0.7)
    assert seen == [True]
