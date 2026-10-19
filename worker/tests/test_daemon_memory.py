import json
import os
import time

import numpy as np
import pytest
from transcriber import daemon, memory
from transcriber.config import Config
from tests.conftest import make_session, silence, to_pcm_bytes, tone
from tests.test_cli import fake_models


def _session(sessions, session_id="2026-09-15-1430"):
    d = make_session(sessions, session_id)
    (d / "mic.pcm").write_bytes(to_pcm_bytes(silence(5.5)))
    (d / "sys.pcm").write_bytes(to_pcm_bytes(np.concatenate([tone(1.0, 880), silence(4.5)])))
    return d


def _status(d):
    return json.loads((d / "state.json").read_text())["status"]


def test_footprint_of_this_process_is_measured():
    assert memory.footprint_mb() > 1


def test_daemon_asks_for_restart_when_idle_footprint_is_over_budget(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, idle_restart_mb=3000)
    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 5000.0)
    dm.once()
    assert dm.restart_requested is False      # встреча идёт — не перезапускаемся
    (d / "STOP").touch()
    dm.once()
    assert _status(d) == "finalized"
    assert dm.restart_requested is True


def test_daemon_stays_when_idle_footprint_is_within_budget(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, idle_restart_mb=3000)
    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 2000.0)
    dm.once()
    (d / "STOP").touch()
    dm.once()
    assert dm.restart_requested is False


def test_daemon_does_not_restart_while_another_session_is_live(vault, tmp_path):
    sessions = tmp_path / "sessions"
    a = _session(sessions, "2026-09-15-1430")
    _session(sessions, "2026-09-15-1500")
    cfg = Config(vault_root=vault, sessions_dir=sessions, idle_restart_mb=3000)
    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 5000.0)
    dm.once()
    (a / "STOP").touch()
    dm.once()
    assert _status(a) == "finalized"
    assert dm.restart_requested is False


def test_zero_budget_disables_restart(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, idle_restart_mb=0, busy_restart_mb=0)
    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 50000.0)
    dm.once()
    (d / "STOP").touch()
    dm.once()
    assert dm.restart_requested is False


def test_run_forever_exits_for_launchd_to_restart(vault, tmp_path, monkeypatch):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    dm = daemon.Daemon(cfg, models=fake_models())

    def once():
        dm.restart_requested = True
        return False

    monkeypatch.setattr(dm, "once", once)
    with pytest.raises(SystemExit) as exc:
        dm.run_forever()
    assert exc.value.code == daemon.EXIT_RESTART != 0   # ненулевой: launchd поднимет при любой политике KeepAlive


def test_abandoned_recording_is_stopped_and_finalized(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    old = time.time() - 11 * 60
    for name in ("mic.pcm", "sys.pcm", "meta.json"):
        os.utime(d / name, (old, old))
    cfg = Config(vault_root=vault, sessions_dir=sessions, abandoned_after_min=10)
    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 100.0)
    dm.once()
    assert (d / "STOP").exists()
    dm.once()
    assert _status(d) == "finalized"


def test_live_recording_is_not_treated_as_abandoned(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, abandoned_after_min=10)
    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 100.0)
    dm.once()
    dm.once()
    assert not (d / "STOP").exists()
    assert _status(d) == "recording"


def test_daemon_restarts_mid_session_when_over_the_hard_limit(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, busy_restart_mb=6000)
    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 7000.0)
    dm.once()
    assert _status(d) == "recording"           # сессию не трогаем — продолжит новый процесс
    assert dm.restart_requested is True


def test_daemon_keeps_working_mid_session_under_the_hard_limit(vault, tmp_path):
    sessions = tmp_path / "sessions"
    _session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions, busy_restart_mb=6000)
    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 5000.0)
    dm.once()
    assert dm.restart_requested is False


def test_daemon_rereads_config_when_a_new_session_starts(vault, tmp_path):
    sessions = tmp_path / "sessions"
    a = _session(sessions, "2026-09-15-1430")
    loads = []

    def reload():
        loads.append(1)
        return Config(vault_root=vault, sessions_dir=sessions, min_silence_ms=700 + 100 * len(loads))

    dm = daemon.Daemon(Config(vault_root=vault, sessions_dir=sessions), models=fake_models(),
                       footprint=lambda: 0.0, reload_config=reload)
    dm.once()
    assert dm.cfg.min_silence_ms == 800 and dm.pipes[a].cfg.min_silence_ms == 800
    dm.once()
    assert len(loads) == 1                     # идущая сессия конфиг не перечитывает
    b = _session(sessions, "2026-09-15-1500")
    dm.once()
    assert dm.cfg.min_silence_ms == 900 and dm.pipes[b].cfg.min_silence_ms == 900
    assert dm.pipes[a].cfg.min_silence_ms == 800


def test_daemon_keeps_previous_config_when_reload_fails(vault, tmp_path):
    sessions = tmp_path / "sessions"
    _session(sessions)
    cfg = Config(vault_root=vault, sessions_dir=sessions)

    def broken():
        raise ValueError("сломанный config.yaml")

    dm = daemon.Daemon(cfg, models=fake_models(), footprint=lambda: 0.0, reload_config=broken)
    dm.once()
    assert dm.cfg is cfg and dm.pipes


def _count_loads(monkeypatch):
    loads = []
    real = daemon.Session.load

    def counting(path):
        loads.append(path.name)
        return real(path)

    monkeypatch.setattr(daemon.Session, "load", staticmethod(counting))
    return loads


def test_finalized_session_is_not_reloaded_on_every_pass(vault, tmp_path, monkeypatch):
    # 117 завершённых сессий грузились целиком (state.json + embeddings.npz) каждую секунду: 75% CPU
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    (d / "STOP").touch()
    dm = daemon.Daemon(Config(vault_root=vault, sessions_dir=sessions), models=fake_models(),
                       footprint=lambda: 0.0)
    dm.once()
    assert _status(d) == "finalized"
    loads = _count_loads(monkeypatch)
    for _ in range(5):
        dm.once()
    assert len(loads) <= 1                     # один раз — узнать, что она завершена, дальше не трогаем


def test_finalized_session_from_before_start_is_loaded_once(vault, tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    (d / "STOP").touch()
    daemon.Daemon(Config(vault_root=vault, sessions_dir=sessions), models=fake_models(),
                  footprint=lambda: 0.0).once()
    dm = daemon.Daemon(Config(vault_root=vault, sessions_dir=sessions), models=fake_models(),
                       footprint=lambda: 0.0)      # перезапуск воркера: сессия уже завершена
    loads = _count_loads(monkeypatch)
    for _ in range(5):
        dm.once()
    assert loads == [d.name]


def test_reopened_finalized_session_is_picked_up_again(vault, tmp_path):
    sessions = tmp_path / "sessions"
    d = _session(sessions)
    (d / "STOP").touch()
    dm = daemon.Daemon(Config(vault_root=vault, sessions_dir=sessions), models=fake_models(),
                       footprint=lambda: 0.0)
    dm.once()
    dm.once()
    assert _status(d) == "finalized" and d not in dm.pipes
    state = json.loads((d / "state.json").read_text())
    state["status"] = "recording"              # так сессию снова открывает CLI
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False))
    (d / "STOP").unlink()
    dm.once()
    assert d in dm.pipes
