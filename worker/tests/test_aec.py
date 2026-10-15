"""Подавление эха проверяем на синтетике: тут точно известно, что эхо, а что владелец.

Сигнал «как речь» — шум в полосе, нарезанный всплесками: у него есть огибающая, по которой
подавитель ищет задержку, и широкий спектр, по которому оценивает усиление тракта.
"""

import numpy as np
import pytest
from transcriber.aec import EchoSuppressor

SR = 16000
ECHO_DELAY = 480        # 30 мс — столько звук идёт от динамика до микрофона на реальной записи
ECHO_GAIN = 0.3


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def energy(x: np.ndarray) -> float:
    return float(np.sum(np.asarray(x, dtype=np.float64) ** 2))


def db(before: float, after: float) -> float:
    return 10.0 * np.log10(before / max(after, 1e-30))


def band_noise(seconds: float, low: float, high: float, amp: float = 0.2,
               seed: int = 0, sr: int = SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    spec = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    spec[(freqs < low) | (freqs > high)] = 0
    x = np.fft.irfft(spec, n)
    return (amp * x / (rms(x) + 1e-12)).astype(np.float32)


def burst_envelope(n: int, seed: int = 0, sr: int = SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    env = np.zeros(n, dtype=np.float32)
    i = 0
    while i < n:
        on = int(rng.uniform(0.6, 1.6) * sr)
        env[i:i + on] = 1.0
        i += on + int(rng.uniform(0.25, 0.7) * sr)
    ramp = max(1, int(0.02 * sr))
    return np.convolve(env, np.ones(ramp) / ramp, mode="same").astype(np.float32)


def speech_like(seconds: float, low: float = 200.0, high: float = 3800.0, amp: float = 0.2,
                seed: int = 0, sr: int = SR) -> np.ndarray:
    x = band_noise(seconds, low, high, amp=1.0, seed=seed, sr=sr) * burst_envelope(
        int(seconds * sr), seed=seed + 100, sr=sr)
    return (amp * x / (rms(x) + 1e-12)).astype(np.float32)


def delayed(x: np.ndarray, samples: int) -> np.ndarray:
    return np.concatenate([np.zeros(samples, dtype=np.float32), x])[:len(x)]


def band_energy(x: np.ndarray, low: float, high: float, sr: int = SR) -> float:
    spec = np.fft.rfft(np.asarray(x, dtype=np.float64))
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    mask = (freqs >= low) & (freqs <= high)
    return float(np.sum(np.abs(spec[mask]) ** 2))


# ---- (a) чистое эхо ----------------------------------------------------------


def test_pure_echo_is_suppressed_by_at_least_15_db():
    """Микрофон слышит только динамики — от дорожки должен остаться шум измерения."""
    sys_ = speech_like(40.0, seed=1)
    mic = (ECHO_GAIN * delayed(sys_, ECHO_DELAY)
           + band_noise(40.0, 100.0, 7000.0, amp=0.0005, seed=2))
    out = EchoSuppressor().process(mic, sys_)

    warm = 8 * SR          # первые секунды подавитель холодный: статистики тракта ещё нет
    assert db(energy(mic[warm:len(out)]), energy(out[warm:])) >= 15.0
    assert db(energy(mic[:len(out)]), energy(out)) >= 10.0   # и по всей записи заметно тише


# ---- (b) владелец в тишине ---------------------------------------------------


def test_owner_speech_passes_untouched_when_speakers_are_silent():
    sys_ = np.concatenate([speech_like(22.0, seed=3), np.zeros(6 * SR, dtype=np.float32)])
    own = np.zeros(len(sys_), dtype=np.float32)
    own[24 * SR:26 * SR] = speech_like(2.0, low=300.0, high=3000.0, amp=0.15, seed=4)
    mic = ECHO_GAIN * delayed(sys_, ECHO_DELAY) + own
    out = EchoSuppressor().process(mic, sys_)

    win = slice(24 * SR, 26 * SR)
    assert abs(db(energy(mic[win]), energy(out[win]))) <= 1.0


# ---- (c) двойной разговор ----------------------------------------------------


OWN_BAND = (300.0, 800.0)
ECHO_BAND = (1000.0, 3000.0)


def test_double_talk_keeps_the_owner_band_and_kills_the_echo_band():
    """Владелец говорит поверх динамиков: его полоса остаётся, полоса эха уходит."""
    sys_ = speech_like(30.0, low=ECHO_BAND[0], high=ECHO_BAND[1], amp=0.25, seed=5)
    echo = ECHO_GAIN * delayed(sys_, ECHO_DELAY)
    own = np.zeros(len(sys_), dtype=np.float32)
    own[20 * SR:24 * SR] = band_noise(4.0, *OWN_BAND, amp=rms(echo), seed=6)
    mic = echo + own
    out = EchoSuppressor().process(mic, sys_)

    win = slice(21 * SR, 23 * SR)          # внутри всплеска, без краёв
    kept = band_energy(out[win], *OWN_BAND) / band_energy(own[win], *OWN_BAND)
    assert kept >= 0.7
    assert db(band_energy(mic[win], *ECHO_BAND), band_energy(out[win], *ECHO_BAND)) >= 10.0


# ---- (d) состояние между вызовами -------------------------------------------


def test_chunked_processing_matches_the_whole_signal():
    """Конвейер зовёт process() кусками по мере записи — результат не должен от этого зависеть."""
    sys_ = speech_like(20.0, seed=7)
    mic = ECHO_GAIN * delayed(sys_, ECHO_DELAY) + speech_like(20.0, amp=0.05, seed=8)

    whole = EchoSuppressor().process(mic, sys_)
    chunked = EchoSuppressor()
    parts = [chunked.process(mic[i:i + 7919], sys_[i:i + 7919]) for i in range(0, len(mic), 7919)]
    joined = np.concatenate(parts)

    assert len(joined) == len(whole)
    assert np.allclose(joined, whole, atol=1e-5)


def test_flush_returns_the_buffered_tail_without_padding():
    sys_ = speech_like(5.0, seed=9)
    mic = ECHO_GAIN * delayed(sys_, ECHO_DELAY)
    sup = EchoSuppressor()
    body = sup.process(mic, sys_)
    tail = sup.flush()
    assert len(body) + len(tail) == len(mic)
    assert sup.flush().size == 0


# ---- (e) пустая дорожка динамиков -------------------------------------------


def test_silent_sys_track_leaves_the_microphone_alone():
    mic = speech_like(6.0, seed=10)
    sup = EchoSuppressor()
    out = np.concatenate([sup.process(mic, np.zeros_like(mic)), sup.flush()])
    assert len(out) == len(mic)
    assert np.allclose(out, mic, atol=1e-6)


def test_tracks_of_different_length_are_a_programming_error():
    sup = EchoSuppressor()
    with pytest.raises(ValueError):
        sup.process(np.zeros(100, dtype=np.float32), np.zeros(99, dtype=np.float32))


def test_estimated_lag_and_gain_match_the_synthetic_echo_path():
    """Задержка попадает в свои 30 мс, усиление — в порядок величины заданных 0.3.

    Точного 0.3 тут и не ждём: низкий процентиль намеренно занижает оценку (чистому эху
    соответствует нижняя мода отношения), недобор компенсирует beta.
    """
    sys_ = speech_like(40.0, seed=11)
    mic = ECHO_GAIN * delayed(sys_, ECHO_DELAY)
    sup = EchoSuppressor()
    sup.process(mic, sys_)
    assert abs(sup.lag_ms - ECHO_DELAY / SR * 1000) <= 20.0
    assert sup.gain is not None
    freqs = np.fft.rfftfreq(sup.n_fft, 1 / SR)
    voiced = sup.gain[(freqs >= 300.0) & (freqs <= 3500.0)]   # полосы, где динамики звучали
    assert 0.5 * ECHO_GAIN <= float(np.median(voiced)) <= 1.5 * ECHO_GAIN


def test_effective_beta_grows_with_the_tract_gain_and_is_capped():
    from transcriber.aec import effective_beta
    assert effective_beta(0.13, 2.0, 3.0) == 2.0            # обычные динамики — как было
    assert abs(effective_beta(0.275, 2.0, 3.0) - 2.5) < 1e-9   # середина — середина
    assert effective_beta(0.40, 2.0, 3.0) == 3.0
    assert effective_beta(0.90, 2.0, 3.0) == 3.0            # выше громкого порога не растёт
    assert effective_beta(0.40, 2.0, 0.0) == 2.0            # выключено
    assert effective_beta(0.40, 2.0, 2.0) == 2.0


def test_suppressor_uses_the_loud_beta_once_the_gain_is_known():
    import numpy as np
    from transcriber.aec import EchoSuppressor
    s = EchoSuppressor(beta=2.0, beta_loud=3.0)
    assert s.current_beta == 2.0
    s._gain = np.full(s.bins, 0.4, dtype=np.float32)
    assert s.current_beta == 3.0
    s._gain = np.full(s.bins, 0.13, dtype=np.float32)
    assert s.current_beta == 2.0
