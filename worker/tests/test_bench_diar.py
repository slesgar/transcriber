import json
from dataclasses import asdict, replace
from pathlib import Path

from transcriber import bench_diar
from transcriber.bench_diar import (aggregate, evaluate_session, format_table, run_bench_diar,
                                     session_metrics)
from transcriber.config import Config
from transcriber.session import Rec, State

from .conftest import make_session, one_hot

ANNA, IVAN, MARIA = "Анна Петрова", "Иван Сидоров", "Мария Иванова"


# ---- чистые метрики ------------------------------------------------------------


def test_session_metrics_is_perfect_when_each_name_owns_one_cluster():
    pairs = [(0, ANNA)] * 4 + [(1, IVAN)] * 3
    m = session_metrics(pairs, n_clusters=2, participants=2)
    assert m["purity"] == 1.0 and m["completeness"] == 1.0
    assert m["merges"] == 0 and m["splits"] == 0
    assert m["labeled"] == 7 and m["names"] == 2 and m["clusters"] == 2 and m["participants"] == 2


def test_session_metrics_counts_a_merge_and_a_split():
    # кластер 0 смешивает Анну и Ивана поровну — склейка; Мария разбита на кластеры 1 и 2 — раскол
    pairs = [(0, ANNA)] * 5 + [(0, IVAN)] * 5 + [(1, MARIA)] * 3 + [(2, MARIA)] * 3
    m = session_metrics(pairs, n_clusters=3, participants=3)
    assert m["purity"] == round(11 / 16, 4)
    assert m["completeness"] == round(13 / 16, 4)
    assert m["merges"] == 1 and m["splits"] == 1


def test_session_metrics_of_nothing_labeled_is_empty_not_a_crash():
    m = session_metrics([], n_clusters=4, participants=1)
    assert m["labeled"] == 0 and m["purity"] is None and m["completeness"] is None


def test_aggregate_weights_by_labeled_records():
    rows = [{"labeled": 10, "purity": 1.0, "completeness": 0.5, "merges": 0, "splits": 1, "clusters": 2, "participants": 2, "names": 2},
            {"labeled": 30, "purity": 0.5, "completeness": 1.0, "merges": 2, "splits": 0, "clusters": 5, "participants": 3, "names": 3}]
    t = aggregate(rows)
    assert t["sessions"] == 2 and t["labeled"] == 40
    assert t["purity"] == round((10 * 1.0 + 30 * 0.5) / 40, 4)
    assert t["completeness"] == round((10 * 0.5 + 30 * 1.0) / 40, 4)
    assert t["merges"] == 2 and t["splits"] == 1 and t["clusters"] == 7 and t["participants"] == 5 and t["names"] == 5


# ---- сессия на диске -------------------------------------------------------------


def _write_session(sessions: Path, session_id: str, names: list[str], per_name: int = 6,
                   private: bool = False) -> Path:
    """Сессия, где каждое имя говорит своим тоном: эмбеддинг one-hot по имени, плитка — по времени."""
    d = make_session(sessions, session_id, participants=names, private=private)
    records, embeddings, samples = [], [], []
    t = 0.0
    for k in range(per_name):
        for i, name in enumerate(names):
            samples.append({"t": t, "participants": [{"name": n, "mic": False, "active": n == name} for n in names]})
            records.append(Rec("sys", t + 0.1, t + 2.9, "…", "Speaker 1", emb=len(embeddings)))
            embeddings.append(one_hot(100 + 100 * i))
            t += 3.0
    (d / "talk.jsonl").write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n")
    st = State(status="finalized", records=records, embeddings=embeddings, speaker_of_emb=[0] * len(embeddings))
    (d / "state.json").write_text(json.dumps(asdict(st), ensure_ascii=False))
    return d


def test_evaluate_session_reads_state_and_talk(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions")
    d = _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN])
    row = evaluate_session(cfg, d)
    assert row["id"] == "2026-09-15-1430" and row["labeled"] == 12 and row["names"] == 2
    assert row["participants"] == 2 and row["clusters"] == 2
    assert row["purity"] == 1.0 and row["completeness"] == 1.0


def test_evaluate_session_skips_short_utterances_below_min_embed_s(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions", min_embed_s=5.0)
    d = _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN])
    row = evaluate_session(cfg, d)
    assert row["labeled"] == 0 and row["clusters"] == 0     # короткие реплики не кластеризуются вовсе


def test_short_utterances_do_not_shape_the_clusters(tmp_path):
    # короткая реплика Ивана с «тоном Анны»: при min_embed_s=1 она склеивает людей, при 2.5 — исключена из кластеров
    cfg = Config(sessions_dir=tmp_path / "sessions")
    d = _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN])
    st = json.loads((d / "state.json").read_text())
    t0 = st["records"][-1]["end"] + 1.0
    st["records"].append({"channel": "sys", "start": t0, "end": t0 + 1.5, "text": "…", "speaker": "Speaker 1",
                          "emb": len(st["embeddings"])})
    st["embeddings"].append(one_hot(100))
    (d / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    with (d / "talk.jsonl").open("a") as f:
        f.write(json.dumps({"t": t0, "participants": [{"name": ANNA, "mic": False, "active": False},
                                                      {"name": IVAN, "mic": False, "active": True}]}) + "\n")
    assert evaluate_session(replace(cfg, min_embed_s=1.0), d)["purity"] < 1.0
    assert evaluate_session(replace(cfg, min_embed_s=2.5), d)["purity"] == 1.0


def test_evaluate_session_returns_none_without_talk_or_embeddings(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions")
    d = make_session(cfg.sessions_dir, "2026-09-15-1430")
    assert evaluate_session(cfg, d) is None
    d2 = _write_session(cfg.sessions_dir, "2026-09-15-1500", [ANNA, IVAN], private=True)
    assert evaluate_session(cfg, d2) is None


# ---- прогон ---------------------------------------------------------------------


def test_run_writes_metrics_and_touches_no_session(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN], per_name=12)
    _write_session(cfg.sessions_dir, "2026-09-16-1000", [ANNA, IVAN, MARIA], per_name=8)
    _write_session(cfg.sessions_dir, "2026-09-17-1000", [ANNA], per_name=30)       # одно имя — вне отбора
    before = {p: p.read_bytes() for p in cfg.sessions_dir.rglob("*") if p.is_file()}
    run = run_bench_diar(cfg, "current-main", note="базовая линия")
    assert run == cfg.bench_dir / "diar" / "runs" / "00-current-main"
    m = json.loads((run / "metrics.json").read_text())
    assert [s["id"] for s in m["sessions"]] == ["2026-09-15-1430", "2026-09-16-1000"]
    assert m["totals"]["sessions"] == 2 and m["totals"]["labeled"] == 48 and m["totals"]["purity"] == 1.0
    assert (run / "config.yaml").exists() and (run / "commit.txt").exists()
    assert (run / "note.txt").read_text() == "базовая линия"
    assert {p: p.read_bytes() for p in cfg.sessions_dir.rglob("*") if p.is_file()} == before


def test_run_applies_overrides_and_filters(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN], per_name=12)
    _write_session(cfg.sessions_dir, "2026-09-16-1000", [ANNA, IVAN], per_name=12)
    run = run_bench_diar(cfg, "later", overrides={"speaker_similarity": 0.55}, since="2026-09-16",
                         min_labeled=5)
    m = json.loads((run / "metrics.json").read_text())
    assert [s["id"] for s in m["sessions"]] == ["2026-09-16-1000"]
    assert m["config"]["speaker_similarity"] == 0.55 and m["selection"]["min_labeled"] == 5
    run2 = run_bench_diar(cfg, "only-one", sessions=["2026-09-15-1430"], min_labeled=5)
    assert [s["id"] for s in json.loads((run2 / "metrics.json").read_text())["sessions"]] == ["2026-09-15-1430"]


def test_run_survives_foreign_dirs_and_filters_since_with_suffixed_ids(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN], per_name=12)
    _write_session(cfg.sessions_dir, "2026-09-16-1000-1", [ANNA, IVAN], per_name=12)
    (cfg.sessions_dir / "record-test" / "2026-09-01-0000").mkdir(parents=True)     # не сессия
    (cfg.sessions_dir / "broken").mkdir()
    (cfg.sessions_dir / "broken" / "talk.jsonl").write_text("{}\n")
    (cfg.sessions_dir / "broken" / "state.json").write_text("{}")                   # нет meta.json
    run = run_bench_diar(cfg, "since", since="2026-09-16")
    assert [s["id"] for s in json.loads((run / "metrics.json").read_text())["sessions"]] == ["2026-09-16-1000-1"]


def test_run_refuses_when_no_session_qualifies(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN], per_name=2)
    try:
        run_bench_diar(cfg, "empty")
    except ValueError as e:
        assert "ни одной" in str(e)
    else:
        raise AssertionError("ожидался ValueError")


def test_table_lists_every_run(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN], per_name=12)
    run_bench_diar(cfg, "a")
    run_bench_diar(cfg, "b", overrides={"speaker_similarity": 0.55})
    text = format_table(bench_diar.collect_rows(cfg.bench_dir / "diar" / "runs"))
    assert "00-a" in text and "01-b" in text and "чистота" in text and "кластеров/говоривших" in text


def test_cli_bench_diar_prints_run_and_table(tmp_path, capsys, monkeypatch):
    from transcriber import cli

    cfg = Config(sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _write_session(cfg.sessions_dir, "2026-09-15-1430", [ANNA, IVAN], per_name=12)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    cli.main(["bench-diar", "--label", "current-main", "--set", "speaker_similarity=0.45", "--min-labeled", "10"])
    out = capsys.readouterr().out
    assert "00-current-main" in out and "чистота" in out
    assert json.loads((cfg.bench_dir / "diar/runs/00-current-main/metrics.json").read_text())["config"]["speaker_similarity"] == 0.45


def test_cli_bench_diar_reports_a_bad_set_as_exit_2(tmp_path, capsys, monkeypatch):
    from transcriber import cli

    cfg = Config(sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    try:
        cli.main(["bench-diar", "--label", "x", "--set", "nope=1"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("ожидался выход 2")


def _merged_session(sessions: Path, session_id: str = "2026-09-15-1430") -> Path:
    """Два человека с одним и тем же тоном: голосом их не развести, плитки Толка — разводят."""
    d = make_session(sessions, session_id, participants=[ANNA, IVAN])
    records, embeddings, samples = [], [], []
    for i in range(24):
        name = ANNA if (i // 2) % 2 == 0 else IVAN          # А,А,И,И,…: оба имени в обеих половинах holdout
        t = 3.0 * i
        samples.append({"t": t, "participants": [{"name": n, "mic": False, "active": n == name} for n in (ANNA, IVAN)]})
        records.append(Rec("sys", t + 0.1, t + 2.9, "…", "Speaker 1", emb=i))
        # голоса похожи (косинус 0.8 > порога 0.4 — один кластер), но не одинаковы: остаток после разреза
        # уходит к своей половине по центроиду
        embeddings.append([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] if name == ANNA
                          else [0.8, 0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    (d / "talk.jsonl").write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n")
    st = State(status="finalized", records=records, embeddings=embeddings, speaker_of_emb=[0] * 24)
    (d / "state.json").write_text(json.dumps(asdict(st), ensure_ascii=False))
    return d


def test_split_by_tiles_is_measured_honestly_with_holdout(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions")
    d = _merged_session(cfg.sessions_dir)
    plain = evaluate_session(replace(cfg, talk_split_share=0.0), d)
    assert plain["clusters"] == 1 and plain["merges"] == 1 and plain["purity"] == 0.5
    split_all = evaluate_session(cfg, d)                          # круговая: режем и меряем по одним именам
    assert split_all["clusters"] == 2 and split_all["purity"] == 1.0 and split_all["labeled"] == 24
    held = evaluate_session(cfg, d, holdout=True)                 # честная: режем по чётным, меряем по нечётным
    assert held["labeled"] == 12 and held["clusters"] == 2
    assert held["purity"] == 1.0 and held["merges"] == 0


def test_holdout_run_is_marked_in_the_table(tmp_path):
    cfg = Config(sessions_dir=tmp_path / "sessions", bench_dir=tmp_path / "bench")
    _merged_session(cfg.sessions_dir)
    run_bench_diar(cfg, "split", holdout=True, min_labeled=5)
    rows = bench_diar.collect_rows(cfg.bench_dir / "diar" / "runs")
    assert rows[0]["label"] == "00-split (½)" and rows[0]["labeled"] == 12
    assert json.loads((cfg.bench_dir / "diar/runs/00-split/metrics.json").read_text())["selection"]["holdout"] is True
