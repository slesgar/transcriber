from transcriber.asr import Segment, build_prompt, clean_text


def test_clean_strips_hallucinations_and_space():
    h = ["Продолжение следует", "Субтитры сделал DimaTorzok"]
    assert clean_text("  Мы посчитаем.  Продолжение следует... ", h) == "Мы посчитаем."
    assert clean_text("Субтитры сделал DimaTorzok", h) == ""
    assert clean_text("субтитры сделал dimatorzok", h) == ""
    assert clean_text("Обычный текст", h) == "Обычный текст"


def test_prompt_is_terms_only_by_default():
    """Имена — главный источник утечки промпта, поэтому по умолчанию их в промпте нет."""
    assert build_prompt(["Иван Сидоров"], ["CDP", "Омни"]) == "CDP, Омни."
    assert build_prompt(["Иван Сидоров"], []) == ""


def test_prompt_adds_participants_only_when_asked():
    p = build_prompt(["Иван Сидоров"], ["CDP"], include_participants=True)
    assert p == "Owner, Иван Сидоров. CDP."
    assert build_prompt([], ["CDP"], include_participants=True) == "CDP."


def test_prompt_caps_glossary_at_forty_eight_terms():
    p = build_prompt([], [f"термин{i}" for i in range(200)])
    assert p.startswith("термин0,") and "термин47" in p and "Термины" not in p
    assert "термин48" not in p and len(p) <= 600


def test_prompt_holds_terms_and_a_score_of_surnames():
    terms = [f"термин{i}" for i in range(12)]
    names = [f"Фамилия{i}" for i in range(20)] + [f"Система{i}" for i in range(14)]
    p = build_prompt([], terms + names)
    assert "термин11" in p and "Фамилия19" in p and "Система13" in p and len(p) <= 600


def test_prompt_caps_even_without_comma_in_truncated_prefix():
    long_term = "x" * 700
    p = build_prompt([], [long_term])
    assert len(p) <= 600


def test_clean_leaves_no_punctuation_debris():
    h = ["Продолжение следует"]
    assert clean_text("Ок, продолжение следует, дальше", h) == "Ок, дальше"


def _transcriber(monkeypatch, result):
    """Transcriber с подменённым mlx_whisper — настоящую модель не грузим."""
    import sys

    from transcriber.asr import Transcriber

    class FakeWhisper:
        def __init__(self):
            self.kwargs = None

        def transcribe(self, audio, **kw):
            self.kwargs = kw
            return result

    fake = FakeWhisper()
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    return Transcriber("model", "ru", ["Продолжение следует"]), fake


def _seg(text, start=0.0, end=1.0, no_speech=0.0, logprob=-0.3):
    return {"text": text, "start": start, "end": end, "no_speech_prob": no_speech, "avg_logprob": logprob}


def test_transcriber_returns_a_segment_per_whisper_segment(monkeypatch):
    import numpy as np

    result = {"text": " Первое. Второе.", "segments": [
        _seg(" Первое.", 0.0, 1.5), _seg(" Второе.", 2.0, 3.5)]}
    t, fake = _transcriber(monkeypatch, result)
    got = t(np.zeros(16000, dtype="float32"), "промпт")
    assert got == [Segment(0.0, 1.5, "Первое."), Segment(2.0, 3.5, "Второе.")]
    assert fake.kwargs["initial_prompt"] == "промпт"
    assert fake.kwargs["condition_on_previous_text"] is True   # блок длинный, контекст внутри него нужен


def test_transcriber_drops_silent_and_low_confidence_segments(monkeypatch):
    import numpy as np

    result = {"text": "", "segments": [
        _seg(" Первое.", 0.0, 1.0),
        _seg(" Тишина.", 1.0, 2.0, no_speech=0.9),
        _seg(" Бред.", 2.0, 3.0, logprob=-1.5),
        _seg(" Второе.", 3.0, 4.0),
    ]}
    t, _ = _transcriber(monkeypatch, result)
    assert [s.text for s in t(np.zeros(16000, dtype="float32"))] == ["Первое.", "Второе."]


def test_transcriber_cleans_hallucinations_and_drops_empty_segments(monkeypatch):
    import numpy as np

    result = {"text": "", "segments": [
        _seg(" Продолжение следует", 0.0, 1.0), _seg(" Есть текст.", 1.0, 2.0)]}
    t, _ = _transcriber(monkeypatch, result)
    assert [s.text for s in t(np.zeros(16000, dtype="float32"))] == ["Есть текст."]


def test_transcriber_falls_back_to_one_segment_over_the_whole_block(monkeypatch):
    import numpy as np

    t, _ = _transcriber(monkeypatch, {"text": " Есть текст. Продолжение следует"})
    assert t(np.zeros(32000, dtype="float32")) == [Segment(0.0, 2.0, "Есть текст.")]


def test_transcriber_survives_segments_without_time_and_quality_fields(monkeypatch):
    import numpy as np

    t, _ = _transcriber(monkeypatch, {"text": " Текст.", "segments": [{"text": " Текст."}]})
    assert t(np.zeros(16000, dtype="float32")) == [Segment(0.0, 0.0, "Текст.")]


def _mlx(monkeypatch):
    """Подменённый mlx.core: записывает лимит кеша и сбросы, Metal не трогаем."""
    import importlib
    import sys
    import types

    fake = types.SimpleNamespace(limits=[], cleared=0)
    fake.set_cache_limit = lambda n: fake.limits.append(n) or 0
    fake.clear_cache = lambda: setattr(fake, "cleared", fake.cleared + 1)
    monkeypatch.setitem(sys.modules, "mlx.core", fake)
    monkeypatch.setattr(importlib.import_module("mlx"), "core", fake, raising=False)
    return fake


def test_transcriber_caps_mlx_buffer_cache_and_releases_it(monkeypatch):
    from transcriber.asr import Transcriber

    _transcriber(monkeypatch, {})            # подменяет mlx_whisper
    mx = _mlx(monkeypatch)
    t = Transcriber("model", "ru", [], cache_mb=512)
    assert mx.limits == [512 * 2 ** 20]
    t.release()
    assert mx.cleared == 1


def test_transcriber_without_cache_limit_never_touches_mlx(monkeypatch):
    import sys

    from transcriber.asr import Transcriber

    _transcriber(monkeypatch, {})
    monkeypatch.setitem(sys.modules, "mlx.core", None)   # импорт упал бы, если бы его попробовали
    Transcriber("model", "ru", []).release()


def test_prompt_names_the_configured_owner():
    assert build_prompt(["Светлана"], [], include_participants=True, owner="Анна Петрова").startswith("Анна Петрова, Светлана.")
