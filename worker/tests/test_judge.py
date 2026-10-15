import json
import random

from transcriber import judge
from transcriber.bench_ref import judge_session, overlap_chunks, parse_local, parse_reference
from transcriber.config import Config
from transcriber.judge import Verdict, judge_chunk, parse_verdict, summarize

ANNA, IVAN = "Анна Петрова", "Иван Сидоров"


def _talk(n_blocks: int = 6, words: int = 40):
    """Две версии одной речи: бот и мы совпадают дословно, но у нас нет первого и последнего блока."""
    blocks = [" ".join(f"тема{b} слово{b}_{i}" for i in range(words)) for b in range(n_blocks)]
    ref = "\n".join(f"[{b:02d}:00] Петрова Анна: {t}." for b, t in enumerate(blocks))
    loc = "## Полный транскрипт\n\n" + "\n\n".join(f"{ANNA}: {t}." for t in blocks[1:-1])
    return parse_reference(ref), parse_local(loc)


def test_overlap_window_ignores_the_tails_only_one_side_recorded():
    ref, loc = _talk()
    chunks, stats = overlap_chunks(ref, loc, chunk_words=10_000)
    assert len(chunks) == 1
    theirs, ours = chunks[0]
    assert "тема0" not in theirs and "тема5" not in theirs and "тема1" in theirs and "тема4" in theirs
    assert stats["window_words_ref"] == stats["window_words_local"] == stats["window_common"]
    assert stats["window_coverage_ref"] == 1.0


def test_overlap_chunks_cut_both_sides_at_the_same_speech():
    ref, loc = _talk(n_blocks=8)
    chunks, _ = overlap_chunks(ref, loc, chunk_words=100)
    assert len(chunks) >= 2
    for theirs, ours in chunks:
        t_topics = {w.split("_")[0] for w in theirs.split() if w.startswith("тема")}
        o_topics = {w.split("_")[0] for w in ours.split() if w.startswith("тема")}
        assert t_topics == o_topics          # один и тот же отрезок речи с обеих сторон


def test_overlap_chunks_empty_without_common_speech():
    ref = parse_reference("[00:01] Петрова Анна: " + " ".join(f"а{i}" for i in range(50)))
    loc = parse_local("## Полный транскрипт\n\nMe: " + " ".join(f"б{i}" for i in range(50)))
    assert overlap_chunks(ref, loc, 100) == ([], {"window_words_ref": 0, "window_words_local": 0, "window_common": 0})


def test_parse_verdict_maps_a_b_back_to_ours_theirs():
    text = 'Вот оценка:\n{"a": {"understanding": 8, "coherence": 7, "garbled": 0.1}, "b": {"understanding": 5, "coherence": 4, "garbled": 0.4}, "better": "a", "missing_in_a": ["цифра бюджета"], "missing_in_b": ["решение по срокам"], "comment": "A полнее"}'
    v = parse_verdict(text, "theirs_first")
    assert v.theirs["understanding"] == 8 and v.ours["understanding"] == 5 and v.better == "theirs"
    assert v.missing_in_theirs == ["цифра бюджета"] and v.missing_in_ours == ["решение по срокам"]
    v2 = parse_verdict(text, "ours_first")
    assert v2.ours["understanding"] == 8 and v2.better == "ours"
    assert parse_verdict("ничего похожего на json", "ours_first") is None
    assert parse_verdict('{"a": {"understanding": "x"}, "b": {}}', "ours_first") is None


def test_judge_chunk_randomizes_order_and_survives_failures():
    seen = []
    def client(prompt):
        a = prompt.split("=== Транскрипт A ===")[1].split("=== Транскрипт B ===")[0]
        seen.append("ours" if "НАШ" in a else "theirs")
        return json.dumps({"a": {"understanding": 9, "coherence": 9, "garbled": 0}, "b": {"understanding": 3, "coherence": 3, "garbled": 0.5}, "better": "a"})
    verdicts = [judge_chunk(client, "НАШ текст", "ИХ текст", rnd=random.Random(i)) for i in range(20)]
    assert {v.better for v in verdicts} == {"ours", "theirs"} and len(set(seen)) == 2   # порядок менялся
    assert all(v.ours["understanding"] in (9, 3) for v in verdicts)
    assert judge_chunk(lambda p: (_ for _ in ()).throw(RuntimeError("сеть")), "a", "b") is None


def test_summarize_counts_wins_and_means():
    vs = [Verdict({"understanding": 8, "coherence": 8, "garbled": 0.1}, {"understanding": 6, "coherence": 7, "garbled": 0.2}, "ours", [], [], "", "ours_first"),
          Verdict({"understanding": 4, "coherence": 5, "garbled": 0.3}, {"understanding": 8, "coherence": 8, "garbled": 0.1}, "theirs", [], [], "", "theirs_first")]
    s = summarize(vs)
    assert s["chunks"] == 2 and s["ours"]["understanding"] == 6.0 and s["theirs"]["coherence"] == 7.5
    assert s["wins_ours"] == 1 and s["wins_theirs"] == 1 and s["equal"] == 0
    assert summarize([]) == {"chunks": 0}


def test_judge_session_scores_every_chunk_of_the_window():
    ref, loc = _talk(n_blocks=8)
    calls = []
    def client(prompt):
        calls.append(prompt)
        return json.dumps({"a": {"understanding": 7, "coherence": 7, "garbled": 0.1}, "b": {"understanding": 7, "coherence": 6, "garbled": 0.2}, "better": "equal", "comment": "похоже"})
    cfg = Config(judge_chunk_words=100)
    res = judge_session(cfg, client, ref, loc, "s-1")
    assert res["chunks"] == len(calls) >= 2 and res["equal"] == res["chunks"]
    assert res["window_common"] > 0 and all("comment" in d for d in res["details"])


def test_cli_judge_without_credentials_exits_2(tmp_path, capsys, monkeypatch):
    from transcriber import cli
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: Config(sessions_dir=tmp_path / "s", bench_dir=tmp_path / "b"))
    try:
        cli.main(["bench-ref", "--label", "x", "--judge"])
    except SystemExit as e:
        assert e.code == 2 and "ANTHROPIC_API_KEY" in capsys.readouterr().err
    else:
        raise AssertionError("ожидался выход 2")


def test_parse_verdict_survives_odd_shapes_and_clamps_scores():
    v = parse_verdict('{"a": {"understanding": "12", "coherence": -1, "garbled": null}, "b": {"understanding": 5, "coherence": 5}, "better": " B ", "missing_in_a": 7, "missing_in_b": "одна строка"}', "ours_first")
    assert v.ours == {"understanding": 10.0, "coherence": 0.0, "garbled": 0.0} and v.better == "theirs"
    assert v.missing_in_ours == [] and v.missing_in_theirs == ["одна строка"]
    assert parse_verdict('{"a": {"understanding": "nan", "coherence": 1}, "b": {"understanding": 1, "coherence": 1}}', "ours_first") is None


def test_adjacent_chunks_do_not_repeat_a_line():
    ref, loc = _talk(n_blocks=8, words=30)
    chunks, _ = overlap_chunks(ref, loc, chunk_words=45)       # рез посреди реплики
    for side in (0, 1):
        lines = [l for t, o in chunks for l in (t, o)[side].splitlines()]
        assert len(lines) == len(set(lines))                   # ни одна реплика не попала в два отрезка
