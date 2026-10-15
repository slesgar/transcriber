"""Подавление эха динамиков по звуку.

Владелец слушает встречу через динамики ноутбука, поэтому микрофон записывает не только
его самого, но и всех собеседников. Что именно играло — известно точно: это дорожка
``sys.pcm``, посэмплово выровненная с ``mic.pcm``. Значит эхо можно снять не по тексту
(текстовый фильтр ``echo.is_echo`` ловит только то, что распозналось на обеих дорожках
одинаково, а распознаётся оно по-разному), а по спектру.

Метод — спектральное вычитание с оценкой тракта:

* обе дорожки разбираются одним STFT (sqrt-ханн, перекрытие 50 %);
* объёмная задержка sys→mic оценивается корреляцией огибающих энергии кадров на 0…``max_lag_ms``
  и сглаживается между переоценками; на живой записи она около 30 мс;
* усиление тракта по полосам ``G[f]`` — НИЗКИЙ процентиль отношения ``|M|/|S|`` по недавним
  кадрам, где sys в этой полосе звучит. Низкий именно потому, что когда говорит ещё и владелец,
  отношение подскакивает: чистому эху соответствует нижняя мода, а не среднее;
* оценка эха ``E = G·|S_задержанный|``, коэффициент подавления по корзинам
  ``H = sqrt(max(floor², 1 − (beta·E/|M|)²))``, на выходе ``ISTFT(H·M)``.

Кадры, где sys молчит, не трогаются вовсе — владелец в тишине проходит как есть. Кадры, от
которых после подавления осталось меньше ``KILL_RATIO`` энергии, глушатся целиком: иначе VAD
цепляется за остаток эха и гонит в ASR шум, который тот охотно превращает в текст. Глушатся,
однако, не поодиночке: после каждого живого кадра ``KILL_RELEASE`` следующих остаются как есть,
потому что дырки по 30 мс посреди слова портят речь вернее любого эха.

Класс потоковый: усиление, задержка и хвост STFT живут между вызовами, поэтому конвейер зовёт
``process()`` кусками по мере записи. Результат от нарезки на куски не зависит — вся статистика
считается по номерам кадров, а не по вызовам. После перезапуска воркера подавитель стартует
холодным: первые секунды проходят без подавления, пока не наберётся статистика тракта.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np

log = logging.getLogger("transcriber")

BIN_FLOOR = 0.02        # |S| в полосе ниже этого — в ней тишина (≈ −60 dBFS при этом окне)
ACTIVE_BINS = 3         # столько звучащих полос — и кадр считается «динамики играют»
GAIN_HISTORY = 400      # кадров в окне оценки усиления (~6.4 с при hop 256)
GAIN_MIN_FRAMES = 60    # меньше — процентиль ещё не статистика, подавление не включаем
GAIN_MIN_BAND = 20      # столько наблюдений нужно полосе, чтобы получить своё усиление
GAIN_EVERY = 50         # шаг переоценки усиления, кадров
GAIN_SMOOTH = 0.7       # доля прежней оценки при обновлении
GAIN_MAX = 2.0          # выше — это уже не эхо, а собственный звук микрофона
LAG_HISTORY = 1000      # кадров в окне оценки задержки (~16 с)
LAG_MIN_ACTIVE = 96     # столько звучащих кадров нужно, чтобы браться за задержку
LAG_EVERY = 250         # шаг переоценки задержки, кадров; кратен GAIN_EVERY
LAG_MIN_CORR = 0.2      # корреляция ниже — пика нет, оставляем прежнюю задержку
LAG_SMOOTH = 0.6
GAIN_NORMAL = 0.15      # медиана усиления тракта при обычной громкости динамиков (стенд 14.10: 0.12–0.14)
GAIN_LOUD = 0.40        # ...и при громких динамиках (12.10 17:01: 0.402) — здесь beta выходит на beta_loud
KILL_RATIO = 0.15       # осталось меньше этой доли энергии кадра — кадр можно глушить целиком
KILL_RELEASE = 8        # ...но не сразу: столько кадров после живого не глушим (~0.25 с)
EPS = 1e-9


def effective_beta(gain_median: float, beta: float, beta_loud: float) -> float:
    """Запас вычитания по громкости тракта: ``beta`` при обычной, линейно до ``beta_loud`` при громкой.

    Стенд 14.10 (тикет 06): одна константа не подходит обеим записям — 3.0 на громкой убирает остаток эха,
    а на обычной режет речь владельца (слов Me −39 %); 2.0 наоборот. ``beta_loud <= beta`` — выключено.
    """
    if beta_loud <= beta or gain_median <= GAIN_NORMAL:
        return beta
    share = min(1.0, (gain_median - GAIN_NORMAL) / (GAIN_LOUD - GAIN_NORMAL))
    return beta + (beta_loud - beta) * share


class EchoSuppressor:
    """Вычитает из микрофона то, что в это время играло в динамиках.

    ``process(mic, sys)`` принимает посэмплово выровненные куски одинаковой длины и возвращает
    очищенный микрофон. Отдаёт он чуть меньше, чем получил: хвост короче окна STFT остаётся в
    буфере до следующего вызова (или до ``flush()``), поэтому сэмплы никогда не обрабатываются
    дважды и нумерация не плывёт — сэмпл i выхода это сэмпл i входа.
    """

    def __init__(self, sr: int = 16000, n_fft: int = 512, hop: int = 256, beta: float = 2.0,
                 floor: float = 0.05, max_lag_ms: float = 400.0, percentile: float = 20.0,
                 beta_loud: float = 0.0):
        if not 0 < hop <= n_fft:
            raise ValueError(f"hop={hop}: нужен шаг в (0, n_fft]")
        self.sr, self.n_fft, self.hop = int(sr), int(n_fft), int(hop)
        self.beta, self.floor, self.percentile = float(beta), float(floor), float(percentile)
        self.beta_loud = float(beta_loud)
        self.bins = self.n_fft // 2 + 1
        self.max_lag = max(1, int(round(max_lag_ms / 1000.0 * self.sr / self.hop)))
        # sqrt-ханн на анализ и на синтез: их произведение — ханн, а он при шаге n_fft/2
        # складывается в единицу, то есть без изменений спектра сигнал собирается обратно точно
        self.win = np.sqrt(np.hanning(self.n_fft + 1)[:self.n_fft]).astype(np.float32)

        overlap = self.n_fft - self.hop
        # предыстория из нулей: первый настоящий сэмпл сразу накрыт полным набором окон,
        # поэтому на выходе нет разгонного участка и выход совпадает со входом по номерам
        self._mbuf = np.zeros(overlap, dtype=np.float32)
        self._sbuf = np.zeros(overlap, dtype=np.float32)
        self._skip = overlap
        self._ola = np.zeros(overlap, dtype=np.float64)
        self._wsum = np.zeros(overlap, dtype=np.float64)
        self._pending = 0                                    # сэмплов на входе, ещё не отданных

        self._shist = np.zeros((self.max_lag + 1, self.bins), dtype=np.float32)
        self._ratios = np.zeros((0, self.bins), dtype=np.float32)
        self._env_m = np.zeros(0, dtype=np.float32)
        self._env_s = np.zeros(0, dtype=np.float32)
        self._env_on = np.zeros(0, dtype=bool)
        self._gain: np.ndarray | None = None
        self._lag = 0
        self._lag_f: float | None = None
        self._frames = 0
        self._hold = 0                                       # кадров отсрочки перед глушением

    # ---- что снаружи видно о состоянии ------------------------------------

    @property
    def gain(self) -> np.ndarray | None:
        """Оценённое усиление тракта по полосам; None — статистики ещё не хватило."""
        return self._gain

    @property
    def current_beta(self) -> float:
        """Действующий запас вычитания: по медиане усиления тракта (см. ``effective_beta``)."""
        if self._gain is None:
            return self.beta
        return effective_beta(float(np.median(self._gain)), self.beta, self.beta_loud)

    @property
    def lag_ms(self) -> float:
        return self._lag * self.hop / self.sr * 1000.0

    # ---- поток -------------------------------------------------------------

    def process(self, mic: np.ndarray, sys: np.ndarray) -> np.ndarray:
        mic = np.asarray(mic, dtype=np.float32).reshape(-1)
        sys = np.asarray(sys, dtype=np.float32).reshape(-1)
        if mic.size != sys.size:
            raise ValueError(f"дорожки разной длины: mic {mic.size}, sys {sys.size}")
        self._pending += mic.size
        out = self._run(mic, sys)
        self._pending -= out.size
        return out

    def flush(self) -> np.ndarray:
        """Хвост, которому не хватило окна: дополняем тишиной — запись кончилась."""
        want = self._pending
        if want <= 0:
            return np.zeros(0, dtype=np.float32)
        pad = np.zeros(self.n_fft, dtype=np.float32)
        out = self._run(pad, pad)[:want]
        self._pending -= out.size
        return out

    # ---- STFT ↔ ISTFT ------------------------------------------------------

    def _run(self, mic: np.ndarray, sys: np.ndarray) -> np.ndarray:
        self._mbuf = np.concatenate([self._mbuf, mic])
        self._sbuf = np.concatenate([self._sbuf, sys])
        n = (len(self._mbuf) - self.n_fft) // self.hop + 1
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        spec_m = self._stft(self._mbuf, n)
        spec_s = self._stft(self._sbuf, n)
        self._mbuf = self._mbuf[n * self.hop:]
        self._sbuf = self._sbuf[n * self.hop:]
        gains = self._frame_gains(np.abs(spec_m), np.abs(spec_s))
        return self._istft(spec_m * gains, n)

    def _stft(self, buf: np.ndarray, n: int) -> np.ndarray:
        idx = np.arange(self.n_fft)[None, :] + (np.arange(n) * self.hop)[:, None]
        return np.fft.rfft(buf[idx] * self.win, axis=1).astype(np.complex64)

    def _istft(self, spec: np.ndarray, n: int) -> np.ndarray:
        frames = np.fft.irfft(spec, self.n_fft, axis=1) * self.win
        span = (n - 1) * self.hop + self.n_fft
        ola = np.zeros(span, dtype=np.float64)
        wsum = np.zeros(span, dtype=np.float64)
        w2 = (self.win.astype(np.float64)) ** 2
        for k in range(n):
            o = k * self.hop
            ola[o:o + self.n_fft] += frames[k]
            wsum[o:o + self.n_fft] += w2
        tail = self.n_fft - self.hop
        ola[:tail] += self._ola
        wsum[:tail] += self._wsum
        done = n * self.hop
        out = ola[:done] / np.maximum(wsum[:done], EPS)
        self._ola, self._wsum = ola[done:], wsum[done:]
        if self._skip:
            cut = min(self._skip, len(out))
            out = out[cut:]
            self._skip -= cut
        return out.astype(np.float32)

    # ---- подавление --------------------------------------------------------

    def _frame_gains(self, mag_m: np.ndarray, mag_s: np.ndarray) -> np.ndarray:
        """Коэффициент на каждую корзину каждого кадра пачки.

        Пачка режется на отрезки по границам переоценки: внутри отрезка усиление и задержка
        постоянны, а границы считаются по сквозному номеру кадра — поэтому нарезка входа на
        куски ничего не меняет.
        """
        hist = np.concatenate([self._shist, mag_s])
        off = len(self._shist)
        out = np.ones((len(mag_m), self.bins), dtype=np.float32)
        start = 0
        while start < len(mag_m):
            end = min(len(mag_m), start + GAIN_EVERY - self._frames % GAIN_EVERY)
            ref = hist[off + start - self._lag:off + end - self._lag]
            bins_on = ref > BIN_FLOOR
            frames_on = bins_on.sum(axis=1) >= ACTIVE_BINS
            if self._gain is not None:
                out[start:end] = self._suppress(mag_m[start:end], ref, frames_on)
            self._observe(mag_m[start:end], mag_s[start:end], ref, bins_on, frames_on)
            self._frames += end - start
            if self._frames % GAIN_EVERY == 0:
                self._estimate_gain()
            if self._frames % LAG_EVERY == 0:
                self._estimate_lag()
            start = end
        self._shist = hist[-(self.max_lag + 1):]
        return out

    def _suppress(self, mag_m: np.ndarray, ref: np.ndarray, frames_on: np.ndarray) -> np.ndarray:
        assert self._gain is not None
        share = self.current_beta * self._gain[None, :] * ref / np.maximum(mag_m, EPS)
        gains = np.sqrt(np.maximum(self.floor ** 2, 1.0 - share ** 2)).astype(np.float32)
        gains[~frames_on] = 1.0
        pre = np.sum(mag_m.astype(np.float64) ** 2, axis=1)
        post = np.sum((gains * mag_m).astype(np.float64) ** 2, axis=1)
        # остаток эха — не речь, но VAD об этом не знает: дешевле обнулить кадр, чем потом
        # вылавливать из транскрипта то, что ASR насочинял на шипении. Одиночные кадры при этом
        # не трогаем: когда владелец говорит поверх динамиков, эхо местами перебивает его на
        # кадр-другой, и дырки в 30 мс посреди слова делают речь нечитаемой вернее любого эха.
        kill = frames_on & (post < KILL_RATIO * pre)
        for i, doomed in enumerate(kill):
            if not doomed:
                self._hold = KILL_RELEASE
            elif self._hold > 0:
                self._hold -= 1
                kill[i] = False
        gains[kill] = 0.0
        return gains

    # ---- оценка тракта -----------------------------------------------------

    def _observe(self, mag_m: np.ndarray, mag_s: np.ndarray, ref: np.ndarray,
                 bins_on: np.ndarray, frames_on: np.ndarray) -> None:
        ratio = np.where(bins_on, mag_m / np.maximum(ref, EPS), np.nan).astype(np.float32)
        self._ratios = np.concatenate([self._ratios, ratio[frames_on]])[-GAIN_HISTORY:]
        self._env_m = np.concatenate(
            [self._env_m, np.sqrt(np.mean(mag_m ** 2, axis=1))])[-LAG_HISTORY:]
        self._env_s = np.concatenate(
            [self._env_s, np.sqrt(np.mean(mag_s ** 2, axis=1))])[-LAG_HISTORY:]
        sys_on = (mag_s > BIN_FLOOR).sum(axis=1) >= ACTIVE_BINS
        self._env_on = np.concatenate([self._env_on, sys_on])[-LAG_HISTORY:]

    def _estimate_gain(self) -> None:
        if len(self._ratios) < GAIN_MIN_FRAMES:
            return
        seen = np.isfinite(self._ratios).sum(axis=0)
        if not (seen >= GAIN_MIN_BAND).any():
            return
        with warnings.catch_warnings():         # полоса без наблюдений — это не беда, а NaN
            warnings.simplefilter("ignore", RuntimeWarning)
            fresh = np.nanpercentile(self._ratios, self.percentile, axis=0)
        fresh = np.clip(fresh, 0.0, GAIN_MAX)
        known = (seen >= GAIN_MIN_BAND) & np.isfinite(fresh)
        # полосам без статистики отдаём медиану остальных: тракт по спектру не рвётся,
        # а на подавление это всё равно не влияет — там, где sys молчит, вычитать нечего
        fill = float(np.median(fresh[known]))
        fresh = np.where(known, fresh, self._gain if self._gain is not None else fill)
        fresh = fresh.astype(np.float32)
        if self._gain is None:
            self._gain = fresh
            log.info("aec: усиление тракта оценено, медиана %.3f, задержка %.0f мс, beta %.2f",
                     float(np.median(fresh)), self.lag_ms, self.current_beta)
        else:
            self._gain = (GAIN_SMOOTH * self._gain + (1.0 - GAIN_SMOOTH) * fresh).astype(np.float32)

    def _estimate_lag(self) -> None:
        if int(self._env_on.sum()) < LAG_MIN_ACTIVE or len(self._env_m) < LAG_MIN_ACTIVE:
            return
        mic = self._env_m - self._env_m.mean()
        sys = self._env_s - self._env_s.mean()
        best_corr, best_lag = -1.0, self._lag
        for lag in range(self.max_lag + 1):
            a, b = mic[lag:], sys[:len(sys) - lag] if lag else sys
            k = min(len(a), len(b))
            if k < LAG_MIN_ACTIVE:
                break
            denom = float(np.linalg.norm(a[:k]) * np.linalg.norm(b[:k]))
            corr = float(np.dot(a[:k], b[:k]) / denom) if denom > EPS else 0.0
            if corr > best_corr:
                best_corr, best_lag = corr, lag
        if best_corr < LAG_MIN_CORR:
            return
        self._lag_f = (float(best_lag) if self._lag_f is None
                       else LAG_SMOOTH * self._lag_f + (1.0 - LAG_SMOOTH) * best_lag)
        self._lag = int(round(self._lag_f))
