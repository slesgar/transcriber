"""Стенд диаризации по реальным сессиям: насколько голосовые кластеры совпадают с плитками Толка.

Эталонной разметки голосов нет, но у каждой записанной встречи есть ``talk.jsonl``: кто был
подсвечен говорящим в какой момент. Это независимая от голоса правда, и по ней видно обе беды
кластеризации — **склейку** (в одном кластере несколько людей) и **раскол** (один человек на
несколько кластеров). Стенд перекластеризует сохранённые эмбеддинги sys-канала текущим
конфигом (тот же путь, что ``refinalize``), размечает реплики именами плиток и считает:

* чистоту — долю размеченных реплик, чьё имя совпало с большинством своего кластера;
* цельность — долю реплик, попавших в главный кластер своего имени;
* склейки и расколы по порогу ``MERGE_SHARE``/``SPLIT_SHARE`` (критерий тикета 04);
* число кластеров против числа говоривших по плиткам (в metrics.json ещё и всех людей на плитках).

Аудио и модели не нужны. Сессии только читаются; прогон ложится в ``bench/diar/runs/NN-<label>/``
рядом со стендом ``bench`` и показывается той же таблицей. Имена в ``metrics.json`` настоящие —
файл живёт в каталоге данных, не в репо.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import numpy as np
import yaml

from .bench import EMPTY, check_label, flat_config, git_commit, next_run_number, run_dir_name, run_dirs
from .config import Config
from .session import Session, State
from .speakers import absorb_small_clusters, recluster, split_by_names
from .talk import MIN_SHARE, TalkFile, is_bot, is_room, is_shared, speaker_for_span, talk_participants

log = logging.getLogger("transcriber")

MERGE_SHARE = 0.3     # два имени с такой долей кластера каждое — кластер склеил людей
SPLIT_SHARE = 0.3     # столько реплик имени вне его главного кластера — человек расколот
MIN_LABELED = 20      # короче сессия ничего не говорит о кластеризации
MIN_NAMES = 2         # с одним собеседником склейки невозможны — такие сессии вне отбора

Pair = tuple[int, str]    # (кластер, имя плитки) одной размеченной реплики


# ---- чистые метрики ------------------------------------------------------------


def session_metrics(pairs: list[Pair], n_clusters: int, participants: int) -> dict:
    """Чистота, цельность, склейки и расколы по парам (кластер, имя). Пусто — метрики None."""
    n = len(pairs)
    out = {"labeled": n, "names": len({name for _, name in pairs}), "clusters": n_clusters,
           "participants": participants, "purity": None, "completeness": None, "merges": 0, "splits": 0}
    if n == 0:
        return out
    by_cluster: dict[int, Counter] = defaultdict(Counter)
    by_name: dict[str, Counter] = defaultdict(Counter)
    for cluster, name in pairs:
        by_cluster[cluster][name] += 1
        by_name[name][cluster] += 1
    out["purity"] = round(sum(c.most_common(1)[0][1] for c in by_cluster.values()) / n, 4)
    out["completeness"] = round(sum(c.most_common(1)[0][1] for c in by_name.values()) / n, 4)
    out["merges"] = sum(1 for c in by_cluster.values()
                        if sum(1 for k in c.values() if k / sum(c.values()) >= MERGE_SHARE) >= 2)
    out["splits"] = sum(1 for c in by_name.values()
                        if (sum(c.values()) - c.most_common(1)[0][1]) / sum(c.values()) >= SPLIT_SHARE)
    return out


def aggregate(rows: list[dict]) -> dict:
    """Итог прогона: доли взвешены числом размеченных реплик, счётчики — суммы."""
    labeled = sum(r["labeled"] for r in rows)

    def weighted(key: str) -> float | None:
        if not labeled:
            return None
        return round(sum(r[key] * r["labeled"] for r in rows if r[key] is not None) / labeled, 4)

    return {"sessions": len(rows), "labeled": labeled, "purity": weighted("purity"),
            "completeness": weighted("completeness"),
            "clusters": sum(r["clusters"] for r in rows), "names": sum(r["names"] for r in rows),
            "participants": sum(r["participants"] for r in rows),
            "merges": sum(r["merges"] for r in rows), "splits": sum(r["splits"] for r in rows)}


# ---- одна сессия ------------------------------------------------------------------


def cluster_labels(cfg: Config, st: State, keep: list[int] | None = None) -> dict[int, int]:
    """Та же перекластеризация, что на финализации (average-linkage по косинусу + слияние мелочи),
    но только по эмбеддингам из ``keep``: {индекс эмбеддинга: кластер}.

    Эмбеддинги в state.json записаны при живой встрече с тем ``min_embed_s``, что был тогда; стенд
    может только отбросить более короткие реплики — и делает это до кластеризации, чтобы прогон с
    ``--set min_embed_s=2`` показывал, какими стали бы кластеры без коротких реплик.
    """
    idx = list(range(len(st.embeddings))) if keep is None else sorted(set(keep))
    if not idx:
        return {}
    embs = [st.embeddings[i] for i in idx]
    labels = absorb_small_clusters(recluster(np.asarray(embs, dtype=np.float32), cfg.speaker_similarity), embs)
    return dict(zip(idx, labels))


def human_participants(talk: TalkFile, cfg: Config) -> list[str]:
    """Люди по плиткам: без ботов, переговорок и общих микрофонов — они кластеров не образуют."""
    return [n for n in talk_participants(talk.samples, cfg.talk_bot_patterns)
            if not is_room(n, cfg.talk_room_patterns) and not is_shared(n, cfg.talk_shared_names)
            and not is_bot(n, cfg.talk_bot_patterns)]


def evaluate_session(cfg: Config, session_dir: Path, holdout: bool = False) -> dict | None:
    """Строка метрик по сессии или None, если по ней нечего мерить (нет плиток, эмбеддингов, приватная).

    ``holdout`` — честная проверка разреза по плиткам: имена чётных реплик идут в разрез,
    метрика считается по нечётным. Без разреза (``talk_split_share: 0``) holdout просто
    ограничивает метрику нечётными репликами, чтобы прогоны были сравнимы.
    """
    talk_path = session_dir / "talk.jsonl"
    if not talk_path.exists() or not (session_dir / "state.json").exists():
        return None
    try:
        session = Session.load(session_dir)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("bench-diar: %s пропущена (%s)", session_dir.name, e)
        return None
    st = session.state
    if session.meta.private or not st.embeddings:
        return None
    talk = TalkFile(talk_path)
    long_enough = [r for r in st.records
                   if r.channel == "sys" and r.emb is not None and 0 <= r.emb < len(st.embeddings)
                   and r.end - r.start >= cfg.min_embed_s]
    labels = cluster_labels(cfg, st, keep=[r.emb for r in long_enough])
    named = [(r, speaker_for_span(talk.timeline, r.start, r.end, MIN_SHARE, cfg.talk_room_patterns,
                                  cfg.talk_bot_patterns, cfg.talk_shared_names)) for r in long_enough]
    if cfg.talk_split_share > 0 and labels:
        # разрез по плиткам: при holdout режем по именам чётных размеченных реплик, а меряем по нечётным —
        # иначе метрика сверяла бы кластеры с теми же именами, по которым их только что разрезали
        idx = sorted(labels)
        pos = {e: k for k, e in enumerate(idx)}
        with_name = [(r, n) for r, n in named if n]
        feed = {r.emb: n for k, (r, n) in enumerate(with_name) if not holdout or k % 2 == 0}
        new, _ = split_by_names([labels[e] for e in idx], [st.embeddings[e] for e in idx],
                                [feed.get(e) for e in idx], cfg.talk_split_share, cfg.talk_split_min_count)
        labels = {e: new[pos[e]] for e in idx}
    with_name = [(r, n) for r, n in named if n]
    pairs: list[Pair] = [(labels[r.emb], n) for k, (r, n) in enumerate(with_name)
                         if not holdout or k % 2 == 1]
    row = session_metrics(pairs, n_clusters=len(set(labels.values())),
                          participants=len(human_participants(talk, cfg)))
    return {"id": session_dir.name, **row}


# ---- отбор и прогон ---------------------------------------------------------------


def qualifies(row: dict, min_labeled: int, min_names: int) -> bool:
    return row["labeled"] >= min_labeled and row["names"] >= min_names


def run_bench_diar(cfg: Config, label: str, overrides: dict | None = None, note: str | None = None,
                   since: str | None = None, sessions: list[str] | None = None,
                   min_labeled: int = MIN_LABELED, min_names: int = MIN_NAMES, holdout: bool = False) -> Path:
    """Прогнать стенд текущим конфигом (плюс ``--set``) и положить результат в новый каталог прогона."""
    check_label(label)
    run_cfg = replace(cfg, **(overrides or {}))
    wanted = set(sessions or [])
    rows: list[dict] = []
    for d in sorted(p for p in cfg.sessions_dir.iterdir() if p.is_dir()):
        if wanted and d.name not in wanted:
            continue
        if since and d.name[:10] < since:
            continue
        row = evaluate_session(run_cfg, d, holdout=holdout)
        if row and qualifies(row, min_labeled, min_names):
            rows.append(row)
    if not rows:
        raise ValueError("bench-diar: ни одной подходящей сессии (нужны talk.jsonl, эмбеддинги, "
                         f"≥ {min_labeled} размеченных реплик и ≥ {min_names} имени)")
    totals = aggregate(rows)
    runs_dir = cfg.bench_dir / "diar" / "runs"
    run = runs_dir / run_dir_name(next_run_number(runs_dir) if runs_dir.exists() else 0, label)
    run.mkdir(parents=True)
    metrics = {"label": label, "totals": totals, "sessions": rows,
               "selection": {"since": since, "sessions": sorted(wanted) or None,
                             "min_labeled": min_labeled, "min_names": min_names, "holdout": holdout},
               "config": {"speaker_similarity": run_cfg.speaker_similarity, "min_embed_s": run_cfg.min_embed_s,
                          "talk_split_share": run_cfg.talk_split_share, "talk_split_min_count": run_cfg.talk_split_min_count,
                          "talk_shared_names": list(run_cfg.talk_shared_names)}}
    (run / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    (run / "config.yaml").write_text(yaml.safe_dump(flat_config(run_cfg), allow_unicode=True, sort_keys=True),
                                     encoding="utf-8")
    (run / "commit.txt").write_text(git_commit() + "\n", encoding="utf-8")
    if note:
        (run / "note.txt").write_text(note, encoding="utf-8")
    log.info("bench-diar: %s — %d сессий, %d реплик, чистота %s, цельность %s", run.name,
             totals["sessions"], totals["labeled"], totals["purity"], totals["completeness"])
    return run


# ---- таблица прогонов ---------------------------------------------------------------


def collect_rows(runs_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for d in run_dirs(runs_dir) if Path(runs_dir).exists() else []:
        f = d / "metrics.json"
        if not f.exists():
            continue
        t = json.loads(f.read_text(encoding="utf-8"))["totals"]
        holdout = json.loads(f.read_text(encoding="utf-8")).get("selection", {}).get("holdout")
        rows.append({"label": d.name + (" (½)" if holdout else ""), "sessions": t["sessions"], "labeled": t["labeled"],
                     "purity": t["purity"], "completeness": t["completeness"],
                     "ratio": f'{t["clusters"]}/{t["names"]}', "merges": t["merges"], "splits": t["splits"]})
    return rows


COLUMNS = (("label", "прогон", "<"), ("sessions", "сессий", ">"), ("labeled", "реплик", ">"),
           ("purity", "чистота", ">"), ("completeness", "цельность", ">"),
           ("ratio", "кластеров/говоривших", ">"), ("merges", "склеек", ">"), ("splits", "расколов", ">"))


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "прогонов пока нет"
    cells = [[str(r.get(key)) if r.get(key) is not None else EMPTY for key, _, _ in COLUMNS] for r in rows]
    widths = [max(len(title), *(len(c[i]) for c in cells)) for i, (_, title, _) in enumerate(COLUMNS)]
    out = ["  ".join(f"{title:{align}{w}}" for (_, title, align), w in zip(COLUMNS, widths))]
    out += ["  ".join(f"{c:{align}{w}}" for c, (_, _, align), w in zip(row, COLUMNS, widths)) for row in cells]
    return "\n".join(out)
