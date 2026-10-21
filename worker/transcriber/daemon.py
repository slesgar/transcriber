from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from . import memory
from .config import Config
from .continuation import attach
from .models import Models, load_models
from .pipeline import Pipeline
from .session import Session, lock_holder
from .agent_qa import QA_FILE, write_agent_qa_safely
from .vault import ensure_stub, write_raw

log = logging.getLogger("transcriber")

FAIL_LIMIT = 5          # столько подряд падений — и сессию откладываем
BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0
DEAD = ("finalized", "failed")
EXIT_RESTART = 75       # EX_TEMPFAIL: «перезапусти меня» — launchd поднимет при любой политике KeepAlive


class Daemon:
    def __init__(self, cfg: Config, models: Models | None = None,
                 footprint: Callable[[], float] = memory.footprint_mb,
                 reload_config: Callable[[], Config] | None = None):
        self.cfg = cfg
        # «Настройки…» правят config.yaml на ходу: конфиг перечитывается при старте следующей сессии
        self.reload_config = reload_config
        self._seen: set[Path] = set()            # сессии, для которых конфиг уже перечитан
        self._models = models
        self.footprint = footprint
        self.restart_requested = False
        self.pipes: dict[Path, Pipeline] = {}
        self.failures: dict[Path, int] = {}      # подряд идущие падения по каталогу
        self.retry_after: dict[Path, float] = {}
        # завершённые сессии → отпечаток их state.json; пока он тот же, сессию не открываем вовсе
        self._dead: dict[Path, tuple[int, int]] = {}

    @property
    def models(self) -> Models:
        if self._models is None:
            log.info("loading models")
            self._models = load_models(self.cfg)
        return self._models

    def once(self) -> bool:
        """One pass over all sessions. Returns True if any session did work."""
        worked = finalized = False
        if not self.cfg.sessions_dir.exists():
            return False
        self._forget_vanished()
        for d in sorted(p for p in self.cfg.sessions_dir.iterdir() if (p / "meta.json").exists()):
            if self._known_dead(d):
                continue
            if self._sleeping(d):
                continue
            self._stop_if_abandoned(d)
            if lock_holder(d) is not None:       # сессию взял CLI (retranscribe и т.п.) — не мешаем
                self.pipes.pop(d, None)
                continue
            try:
                pipe = self.pipes.get(d)
                if pipe is not None and _status_on_disk(d) in DEAD:
                    del self.pipes[d]            # финализировали снаружи (cli finalize)
                    continue
                if pipe is None:
                    stamp = _state_stamp(d)              # до загрузки: запись после неё сбросит отпечаток
                    session = Session.load(d)
                    if session.state.status in DEAD:
                        self._late_answers(d, session)
                        if stamp is not None:
                            self._dead[d] = stamp
                        continue
                    if d not in self._seen:
                        self._seen.add(d)
                        self._reload()
                    if session.state.raw_path is None:
                        # перезапуск той же встречи подхватывает Raw и заметку предыдущей части;
                        # сбой поиска не должен остановить запись — тогда это просто новая встреча
                        try:
                            attach(self.cfg, session)
                        except Exception:  # noqa: BLE001
                            log.exception("%s: поиск продолжения не удался, пишем как новую встречу", d.name)
                        # заметка и Raw должны существовать до первой загрузки моделей (10+ с),
                        # иначе «открыть заметку» в приложении бьётся в пустой note_path
                        ensure_stub(self.cfg, session)
                        write_raw(self.cfg, session, "идёт запись")
                        session.save_state()
                    m = self.models
                    pipe = Pipeline(self.cfg, session, m.asr, m.embed, m.prob_factory)
                    self.pipes[d] = pipe
                worked |= pipe.step() or pipe.has_backlog()    # бэклог читается порциями
                if pipe.s.stop_requested():
                    log.info("finalizing %s", d.name)
                    pipe.finalize()
                    del self.pipes[d]
                    worked = True
                    finalized = True
                self.failures.pop(d, None)
                self.retry_after.pop(d, None)
            except Exception:  # noqa: BLE001 — one broken session must not stop the others
                self._note_failure(d)
        if finalized and not self.pipes:
            self.models.release()                # встреч больше нет — кеш буферов MLX отдаём системе
            self._check_idle_footprint()
        elif self.pipes:
            self._check_busy_footprint()
        return worked

    def _reload(self) -> None:
        """Новая сессия — свежий конфиг; сломанный файл не роняет запись, остаётся прежний."""
        if self.reload_config is None:
            return
        try:
            self.cfg = self.reload_config()
        except Exception:  # noqa: BLE001 — владелец поправит файл, сессия пойдёт со старым конфигом
            log.exception("config reload failed, keeping the previous config")

    # ---- память -----------------------------------------------------------

    def _check_idle_footprint(self) -> None:
        """Встреч нет: если воркер всё равно держит больше бюджета — просим launchd о чистом процессе.

        Кеш MLX ограничен и сбрасывается (asr.py), но за неделю воркер всё равно дорастал до 11 ГБ:
        копится что-то помимо кеша. Перезапуск между встречами стоит ~10 с загрузки моделей
        на следующей встрече и снимает рост, откуда бы он ни шёл.
        """
        used = self.footprint()
        log.info("idle memory: footprint %.0f MB, %s", used, memory.mlx_summary())
        if self.cfg.idle_restart_mb and used > self.cfg.idle_restart_mb:
            log.warning("idle footprint %.0f MB > %d MB — exiting for a clean restart",
                        used, self.cfg.idle_restart_mb)
            self.restart_requested = True

    def _check_busy_footprint(self) -> None:
        """Посреди встречи whisper течёт на ~5 ГБ в час разговора. Сверх busy_restart_mb выходим:
        аудио пишет приложение, а новый процесс продолжит сессию с сохранённых смещений."""
        if not self.cfg.busy_restart_mb:
            return
        used = self.footprint()
        if used > self.cfg.busy_restart_mb:
            log.warning("footprint %.0f MB > %d MB mid-session (%s) — exiting, sessions resume after restart",
                        used, self.cfg.busy_restart_mb, memory.mlx_summary())
            self.restart_requested = True

    def _stop_if_abandoned(self, d: Path) -> None:
        """Запись, чьё аудио давно не растёт, а STOP так и нет, — брошена (приложение упало
        или захват не стартовал). Без STOP она навсегда остаётся в pipes, и между встречами
        не срабатывают ни сброс кеша, ни перезапуск."""
        if not self.cfg.abandoned_after_min or (d / "STOP").exists() or _status_on_disk(d) in DEAD:
            return
        files = [d / n for n in ("mic.pcm", "sys.pcm", "meta.json") if (d / n).exists()]
        last = max((f.stat().st_mtime for f in files), default=0.0)
        if time.time() - last < self.cfg.abandoned_after_min * 60:
            return
        log.warning("session %s: no audio for %d+ min and no STOP — treating as abandoned, finalizing",
                    d.name, self.cfg.abandoned_after_min)
        (d / "STOP").touch()

    # ---- отказоустойчивость ----------------------------------------------

    def _late_answers(self, d: Path, session: Session) -> None:
        """Ответ агента, пришедший после финализации (агент думает до трёх минут, встреча уже
        закончилась), дописывается в файл вопросов: qa.jsonl новее state.json — значит не учтён."""
        if session.state.status != "finalized":
            return
        try:
            qa_m = (d / QA_FILE).stat().st_mtime_ns
            state_m = (d / "state.json").stat().st_mtime_ns
        except OSError:
            return
        if qa_m > state_m:
            write_agent_qa_safely(self.cfg, session)

    def _known_dead(self, d: Path) -> bool:
        """Сессия уже найдена завершённой и её state.json с тех пор не менялся.

        Без этого каждый проход (раз в 0,2–1 с) целиком грузил state.json и embeddings.npz всех
        завершённых сессий — 117 каталогов держали воркер на 75% CPU между встречами, а сессии
        с рассинхроном эмбеддингов писали предупреждение раз в 5 с. CLI, снова открывающий сессию
        (retranscribe), переписывает state.json — отпечаток меняется, и демон смотрит её заново.
        """
        stamp = self._dead.get(d)
        if stamp is None:
            return False
        if _state_stamp(d) == stamp:
            return True
        del self._dead[d]
        return False

    def _forget_vanished(self) -> None:
        for d in [p for p in self.pipes if not (p / "meta.json").exists()]:
            log.info("session %s vanished, dropping", d.name)
            del self.pipes[d]
        self._seen = {p for p in self._seen if (p / "meta.json").exists()}
        self._dead = {p: s for p, s in self._dead.items() if (p / "meta.json").exists()}

    def _sleeping(self, d: Path) -> bool:
        return self.failures.get(d, 0) >= FAIL_LIMIT and time.monotonic() < self.retry_after.get(d, 0.0)

    def _note_failure(self, d: Path) -> None:
        self.pipes.pop(d, None)
        n = self.failures.get(d, 0) + 1
        self.failures[d] = n
        if n < FAIL_LIMIT:
            log.exception("session %s failed (%d/%d)", d.name, n, FAIL_LIMIT)
            return
        delay = min(BACKOFF_MAX_S, BACKOFF_START_S * 2 ** (n - FAIL_LIMIT))
        self.retry_after[d] = time.monotonic() + delay
        log.warning("session %s failed %d times, skipping for %.0fs", d.name, n, delay)
        self._mark_failed(d)

    def _mark_failed(self, d: Path) -> None:
        """Если state.json читается — помечаем сессию failed, чтобы once() обходил её как finalized."""
        try:
            session = Session.load(d)
        except Exception:  # noqa: BLE001 — сломанная meta.json: пометить негде, хватит бэкоффа
            return
        if session.state.status not in DEAD:
            session.state.status = "failed"
            session.save_state()

    def run_forever(self) -> None:
        log.info("worker started, sessions at %s", self.cfg.sessions_dir)
        while not self.restart_requested:
            worked = self.once()
            time.sleep(0.2 if worked else 1.0)
        raise SystemExit(EXIT_RESTART)


def _state_stamp(d: Path) -> tuple[int, int, int, int] | None:
    """state.json и qa.jsonl: новый ответ агента к завершённой встрече тоже повод заглянуть."""
    try:
        st = (d / "state.json").stat()
    except OSError:
        return None
    try:
        qa = (d / QA_FILE).stat()
        qa_part = (qa.st_mtime_ns, qa.st_size)
    except OSError:
        qa_part = (0, 0)
    return st.st_mtime_ns, st.st_size, *qa_part


def _status_on_disk(d: Path) -> str:
    try:
        return str(json.loads((d / "state.json").read_text()).get("status", ""))
    except Exception:  # noqa: BLE001 — нечитаемый state.json разберёт обычный путь обработки
        return ""
