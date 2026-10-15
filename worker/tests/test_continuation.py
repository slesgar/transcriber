import json
from datetime import datetime, timedelta
from pathlib import Path

from transcriber import daemon
from transcriber.config import Config
from transcriber.continuation import attach, chain, find_previous, merged_records
from transcriber.session import Rec, Session, State
from transcriber.vault import write_raw

from .conftest import FakeAsr, FakeEmbedder, fake_prob, make_session

TITLE = "Еженедельный синк"
T0 = datetime.fromisoformat("2026-09-15T14:30:00+03:00")


def _part(sessions: Path, session_id: str, start: datetime, minutes: float, title: str = TITLE,
          status: str = "finalized", speakers=("Me", "Speaker 1"), private: bool = False) -> Session:
    d = make_session(sessions, session_id, title=title, private=private)
    meta = json.loads((d / "meta.json").read_text())
    meta["started_at"] = start.isoformat()
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    s = Session.load(d)
    recs = []
    t = 0.0
    while t < minutes * 60:
        for k, spk in enumerate(speakers):
            recs.append(Rec("mic" if spk == "Me" else "sys", t, t + 5.0, f"{session_id} {spk} {int(t)}", spk))
            t += 10.0
    s.state = State(status=status, records=recs)
    s.save_state()
    return s


def test_find_previous_picks_the_latest_same_title_part_within_the_gap(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions", continue_gap_min=20)
    _part(cfg.sessions_dir, "2026-09-15-1300", T0 - timedelta(minutes=90), 30)         # кончилась за час — далеко
    _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=30), 15)         # кончилась за 15 мин — подходит
    _part(cfg.sessions_dir, "2026-09-15-1410", T0 - timedelta(minutes=20), 10, title="Другая встреча")
    _part(cfg.sessions_dir, "2026-09-15-1415", T0 - timedelta(minutes=15), 5, status="recording")
    _part(cfg.sessions_dir, "2026-09-15-1420", T0 - timedelta(minutes=10), 5, private=True)
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 1, status="recording")
    assert find_previous(cfg, new).meta.id == "2026-09-15-1400"


def test_find_previous_refuses_untitled_meetings_disabled_gap_and_already_continued(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions")
    prev = _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=20), 10)
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 1, status="recording")
    assert find_previous(Config(sessions_dir=cfg.sessions_dir, continue_gap_min=0), new) is None
    untitled = _part(cfg.sessions_dir, "2026-09-15-1431", T0 + timedelta(minutes=1), 1, title="", status="recording")
    _part(cfg.sessions_dir, "2026-09-15-1405", T0 - timedelta(minutes=25), 1, title="")
    assert find_previous(cfg, untitled) is None
    prev.state.continued_by = "кто-то"
    prev.save_state()
    assert find_previous(cfg, new) is None


def test_attach_pins_raw_and_note_and_links_both_ways(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    prev = _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=20), 10)
    raw = write_raw(cfg, prev, "финализирован")
    prev.state.note_path = str(vault / "note.md")
    prev.save_state()
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 1, status="recording")
    assert attach(cfg, new).meta.id == "2026-09-15-1400"
    assert new.state.continues == "2026-09-15-1400" and new.state.raw_path == str(raw)
    assert new.state.note_path == str(vault / "note.md")
    assert Session.load(prev.dir).state.continued_by == "2026-09-15-1430"
    assert [p.meta.id for p in chain(cfg, new)] == ["2026-09-15-1400", "2026-09-15-1430"]
    assert [p.meta.id for p in chain(cfg, Session.load(prev.dir))] == ["2026-09-15-1400", "2026-09-15-1430"]


def test_merged_records_shift_time_renumber_speakers_and_report_the_break(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions")
    a = _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=20), 10, speakers=("Me", "Speaker 1", "Speaker 2"))
    b = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 5, speakers=("Me", "Speaker 1"))
    recs, breaks = merged_records([a, b])
    assert breaks == [(a.state.records[-1].end, 1200.0)]
    first_b = next(r for r in recs if r.text.startswith("2026-09-15-1430"))
    assert first_b.start == 1200.0 and first_b.speaker == "Me"
    assert {r.speaker for r in recs if r.text.startswith("2026-09-15-1430") and r.channel == "sys"} == {"Speaker 3"}
    assert {r.speaker for r in recs if r.text.startswith("2026-09-15-1400") and r.channel == "sys"} == {"Speaker 1", "Speaker 2"}


def test_write_raw_of_a_continuation_produces_one_transcript_with_a_break_marker(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    prev = _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=20), 10)
    raw = write_raw(cfg, prev, "финализирован")
    prev.save_state()
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 3, status="recording")
    attach(cfg, new)
    assert write_raw(cfg, new, "идёт запись") == raw
    text = raw.read_text()
    assert text.count("\n# ") == 1 and "14:10" in text.splitlines()[8]           # шапка первой части
    assert "— запись прервана 00:09, продолжение 00:20 —" in text
    assert "2026-09-15-1400 Speaker 1 10" in text and "2026-09-15-1430 Speaker 1 10" in text
    assert "частей записи: 2" in text
    assert len(list((vault / "Memory/50 · Raw/Local").glob("*.md"))) == 1


def test_daemon_attaches_a_restart_on_first_sight(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", min_embed_s=1.0)
    prev = _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=20), 10)
    write_raw(cfg, prev, "финализирован")
    prev.save_state()
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 0, status="recording")
    (new.dir / "mic.pcm").write_bytes(b"")
    (new.dir / "sys.pcm").write_bytes(b"")
    dm = daemon.Daemon(cfg, models=daemon.Models(asr=FakeAsr(), embed=FakeEmbedder(), prob_factory=lambda: fake_prob))
    dm.once()
    st = Session.load(new.dir).state
    assert st.continues == "2026-09-15-1400" and st.raw_path == prev.state.raw_path
    assert len(list((vault / "Memory/30 · Meetings").glob("*.md"))) == 1          # одна заглушка на встречу


def test_a_corrupt_neighbour_does_not_block_a_new_recording(vault, tmp_path, caplog):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", min_embed_s=1.0)
    broken = make_session(cfg.sessions_dir, "2026-09-15-1300", title=TITLE)
    (broken / "state.json").write_text('{"status": "finalized", "records": "not a list"}')
    (broken / "embeddings.npz").write_bytes(b"junk")
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 0, status="recording")
    (new.dir / "mic.pcm").write_bytes(b"")
    (new.dir / "sys.pcm").write_bytes(b"")
    dm = daemon.Daemon(cfg, models=daemon.Models(asr=FakeAsr(), embed=FakeEmbedder(), prob_factory=lambda: fake_prob))
    with caplog.at_level("WARNING", logger="transcriber"):
        dm.once()
    st = Session.load(new.dir).state
    assert st.raw_path and st.continues is None and new.dir not in dm.failures


def test_attach_is_idempotent_after_a_crash_between_the_two_saves(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    prev = _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=20), 10)
    write_raw(cfg, prev, "финализирован")
    prev.state.continued_by = "2026-09-15-1430"           # предыдущая уже помечена, новая — нет
    prev.save_state()
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 1, status="recording")
    assert attach(cfg, new).meta.id == "2026-09-15-1400"
    other = _part(cfg.sessions_dir, "2026-09-15-1440", T0 + timedelta(minutes=10), 1, status="recording")
    assert find_previous(cfg, other) is None               # чужое продолжение не перехватываем


def test_make_private_refuses_a_part_of_a_chained_meeting(vault, tmp_path):
    from transcriber import cli
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    prev = _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=20), 10)
    write_raw(cfg, prev, "финализирован")
    prev.save_state()
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 1, status="recording")
    attach(cfg, new)
    new.save_state()
    for sid in ("2026-09-15-1400", "2026-09-15-1430"):
        try:
            cli.make_private(cfg, sid)
        except ValueError as e:
            assert "часть встречи" in str(e)
        else:
            raise AssertionError("make-private должен отказать части цепочки")
    assert Path(prev.state.raw_path).exists()


def test_chain_stops_at_a_private_part(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    prev = _part(cfg.sessions_dir, "2026-09-15-1400", T0 - timedelta(minutes=20), 10)
    new = _part(cfg.sessions_dir, "2026-09-15-1430", T0, 1, status="recording")
    new.state.continues = prev.meta.id
    meta = json.loads((prev.dir / "meta.json").read_text()); meta["private"] = True
    (prev.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    assert [p.meta.id for p in chain(cfg, new)] == ["2026-09-15-1430"]
