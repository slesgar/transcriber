from pathlib import Path

import pytest
from transcriber import cli
from transcriber.config import Config, load_config

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.slow
def test_real_models_on_synthetic_voices(vault, tmp_path):
    if not (FIX / "mic.wav").exists():
        pytest.skip("fixtures missing; see plan Task 11")
    base = load_config()
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", hallucinations=base.hallucinations,
                 glossary=base.glossary)
    raw = cli.from_wav(cfg, FIX / "mic.wav", FIX / "sys.wav", title="Дымовой тест", app="manual")
    text = raw.read_text()
    print(text)
    assert "Speaker 1:" in text and "Me:" in text
    assert "параметр" in text.lower() and "подтверждаю" in text.lower()
