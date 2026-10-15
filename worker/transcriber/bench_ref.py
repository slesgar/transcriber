"""Стенд сравнения с корпоративным транскрибатором (бот Толка → заметки бота Толка).

Эталонной разметки у нас нет, но есть второй транскрибатор той же встречи: бот Толка пишет
полный транскрипт с ФИО и таймингами, и скрипт вольта складывает его в ``Memory/50 · Raw/TalkBot/``.
Там, где оба слышали одно и то же, слова совпадают; где расходятся — кто-то ошибся, и это место
стоит послушать. Стенд считает по каждой встрече, записанной обоими:

* слов у эталона и у нас, общих слов (совпавшие цепочки от ``MIN_RUN`` слов);
* покрытие эталона нами (сколько из услышанного ботом услышали и мы) и покрытие нас эталоном;
* согласие говорящих: для каждой нашей метки — доля общих слов, приходящихся на самое частое ФИО
  эталона (та же логика, что у стенда диаризации, только эталон — бот, а не плитки);
* совпадение имён: наша метка-имя и ФИО эталона — один человек (по фамилии);
* самые длинные куски, которые есть только у одной стороны.

Пара «сессия ↔ заметка» ищется по дате, времени старта и названию. Общих слов меньше
``MIN_COMMON`` — это не одна и та же речь (бот писал другую встречу или мы — другой звук),
такая пара в итог не идёт, но остаётся в отчёте как «нет совпадения».
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from .bench import EMPTY, check_label, flat_config, git_commit, next_run_number, run_dir_name, run_dirs, utterances, words_of
from .config import Config
from .session import Session
from . import judge as judge_mod

log = logging.getLogger("transcriber")

REF_LINE = re.compile(r"^\[(\d+):(\d\d)\]\s+(.+?):\s+(.*)$")
REF_NAME = re.compile(r"^(?P<prefix>[^-]+)-(?P<date>\d{4}-\d\d-\d\d)-(?P<hhmm>\d{4})-(?P<title>.*)\.md$")
MIN_RUN = 3           # короче цепочка совпавших слов — случайность, не общая речь
MIN_COMMON = 0.1      # меньше общих слов от меньшей стороны — это разные встречи
MAX_START_DIFF_MIN = 15
MIN_TITLE_SIM = 0.5
TOP_PASSAGES = 8
MIN_PASSAGE_WORDS = 12

Line = tuple[float, str, str]      # (секунды от старта, говорящий, текст)


# ---- разбор ----------------------------------------------------------------------


def parse_reference(text: str) -> list[Line]:
    """Строки ``[mm:ss] Имя Фамилия: текст`` из заметки бота; всё остальное (саммари, шапка) — мимо."""
    out: list[Line] = []
    for raw in text.splitlines():
        m = REF_LINE.match(raw.strip())
        if m:
            out.append((int(m.group(1)) * 60 + int(m.group(2)), m.group(3).strip(), m.group(4).strip()))
    return out


def parse_local(text: str) -> list[Line]:
    """Реплики нашего Raw: время — ближайший маркер сверху."""
    out: list[Line] = []
    for marker, speaker, body in utterances(text):
        h, m = marker.split(":")
        out.append((int(h) * 3600 + int(m) * 60, speaker, body))
    return out


def _norm_title(title: str) -> str:
    return re.sub(r"[^\w]+", " ", title.lower()).strip()


def find_reference(ref_dir: Path, started_at: datetime, title: str) -> Path | None:
    """Заметка бота той же встречи: тот же день, старт в пределах ``MAX_START_DIFF_MIN``, похожее название."""
    if not ref_dir.exists():
        return None
    best: tuple[float, Path] | None = None
    for p in ref_dir.iterdir():
        m = REF_NAME.match(p.name)
        if not m or m.group("date") != started_at.strftime("%Y-%m-%d"):
            continue
        start = int(m.group("hhmm")[:2]) * 60 + int(m.group("hhmm")[2:])
        mine = started_at.hour * 60 + started_at.minute
        if abs(start - mine) > MAX_START_DIFF_MIN:
            continue
        sim = SequenceMatcher(None, _norm_title(m.group("title")), _norm_title(title)).ratio()
        if sim < MIN_TITLE_SIM:
            continue
        score = sim - abs(start - mine) / 1000.0
        if best is None or score > best[0]:
            best = (score, p)
    return best[1] if best else None


# ---- сравнение -------------------------------------------------------------------


def name_tokens(name: str) -> set[str]:
    """Слова имени для сверки: без регистра, ё→е; однобуквенные (инициалы) не считаются."""
    return {p.lower().replace("ё", "е") for p in re.split(r"[\s,.]+", name.strip()) if len(p) >= 2}


def same_person(a: str, b: str) -> bool:
    """Один ли это человек: у бота «Фамилия Имя Отчество», у нас «Имя Фамилия» — совпадение по фамилии,
    а она у обоих в наборе слов; общего имени без фамилии («Иван Петров» и «Иван Сидоров») мало."""
    common = name_tokens(a) & name_tokens(b)
    return len(common) >= 1 and common != (name_tokens(a) & name_tokens(b) & FIRST_NAMES_HINT)


def surname(name: str) -> str:
    """Самое длинное слово имени — для подписи в отчёте, не для сверки (см. ``same_person``)."""
    parts = [p for p in re.split(r"[\s,]+", name.strip()) if p]
    return max(parts, key=len).lower() if parts else ""


# частые имена: совпадение только по ним — не доказательство, что это тот же человек
FIRST_NAMES_HINT = {"александр", "алексей", "андрей", "анна", "антон", "артем", "дмитрий", "евгений", "екатерина",
                    "елена", "иван", "игорь", "илья", "ирина", "мария", "максим", "михаил", "наталья", "николай",
                    "ольга", "павел", "петр", "сергей", "татьяна", "юлия", "юрий"}


def _passage(items: list[tuple[str, int]], lines: list[Line], i1: int, i2: int) -> dict:
    chunk = items[i1:i2]
    t = lines[chunk[0][1]][0]
    return {"at": f"{int(t // 60):02d}:{int(t % 60):02d}", "words": len(chunk),
            "text": " ".join(w for w, _ in chunk[:20]) + (" …" if len(chunk) > 20 else "")}


def _lost_passages(sm: SequenceMatcher, wr, wl, ref: list[Line], loc: list[Line]) -> tuple[list[dict], list[dict]]:
    """Куски только у одной стороны. Совпадения короче ``MIN_RUN`` слов («и», «у нас») — случайность:
    они не рвут пропущенный кусок, а сшиваются с соседями."""
    only_ref: list[dict] = []
    only_loc: list[dict] = []
    r0 = r1 = l0 = l1 = None
    def flush():
        nonlocal r0, r1, l0, l1
        if r0 is not None and r1 - r0 >= MIN_PASSAGE_WORDS:
            only_ref.append(_passage(wr, ref, r0, r1))
        if l0 is not None and l1 - l0 >= MIN_PASSAGE_WORDS:
            only_loc.append(_passage(wl, loc, l0, l1))
        r0 = r1 = l0 = l1 = None
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" and i2 - i1 >= MIN_RUN:
            flush()
            continue
        if i2 > i1:
            r0, r1 = (i1 if r0 is None else r0), i2
        if j2 > j1:
            l0, l1 = (j1 if l0 is None else l0), j2
    flush()
    return only_ref, only_loc


def is_room_like(name: str, room_patterns: list[str] | None = None) -> bool:
    """Переговорка или учётная запись комнаты у бота: имя не человека, сверять фамилии бессмысленно."""
    from .talk import is_room
    return is_room(name, room_patterns) or bool(re.search(r"\d", name))


def _line_text(lines: list[Line], items: list[tuple[str, int]], i1: int, i2: int, after: int = -1) -> tuple[str, int]:
    """Реплики, в которые попали слова ``items[i1:i2]`` — целиком, строкой «Говорящий: текст»; строки до
    ``after`` включительно уже ушли в прошлый отрезок и не повторяются. Второе значение — последняя взятая строка."""
    if i2 <= i1:
        return "", after
    first, last = max(items[i1][1], after + 1), items[i2 - 1][1]
    if last < first:
        return "", after
    return "\n".join(f"{s}: {x}" for _, s, x in lines[first:last + 1]), last


def overlap_chunks(ref: list[Line], loc: list[Line], chunk_words: int) -> tuple[list[tuple[str, str]], dict]:
    """Общее окно (от первого до последнего совпадения) и его разбиение на выровненные отрезки.

    Если мы включили запись позже или выключили раньше — хвосты одной стороны не сравниваются вовсе:
    это не ошибка транскрибатора. Граница отрезка — начало совпавшей цепочки, чтобы обе стороны
    резались в одном и том же месте речи. Второе значение — слова обеих сторон внутри окна и общие.
    """
    wr = [(w.lower(), i) for i, (_, _, x) in enumerate(ref) for w in words_of(x)]
    wl = [(w.lower(), i) for i, (_, _, x) in enumerate(loc) for w in words_of(x)]
    sm = SequenceMatcher(None, [w for w, _ in wr], [w for w, _ in wl], autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= MIN_RUN]
    if not blocks:
        return [], {"window_words_ref": 0, "window_words_local": 0, "window_common": 0}
    a0, b0 = blocks[0].a, blocks[0].b
    a1, b1 = blocks[-1].a + blocks[-1].size, blocks[-1].b + blocks[-1].size
    # точки реза — начала совпавших цепочек и точки внутри длинных цепочек (там стороны выровнены пословно)
    chunk_words = max(1, chunk_words)
    cuts = [(blk.a + k, blk.b + k) for blk in blocks for k in range(0, blk.size, chunk_words)]
    chunks: list[tuple[str, str]] = []
    ca, cb = a0, b0
    la = lb = -1
    for a, b in cuts:
        if a - ca >= chunk_words and a < a1:
            ta, la = _line_text(ref, wr, ca, a, la)
            tb, lb = _line_text(loc, wl, cb, b, lb)
            if ta and tb:
                chunks.append((ta, tb))
            ca, cb = a, b
    ta, la = _line_text(ref, wr, ca, a1, la)
    tb, lb = _line_text(loc, wl, cb, b1, lb)
    if ta and tb:
        chunks.append((ta, tb))
    common = sum(b.size for b in blocks)
    stats = {"window_words_ref": a1 - a0, "window_words_local": b1 - b0, "window_common": common,
             "window_coverage_ref": round(common / (a1 - a0), 4) if a1 > a0 else None,
             "window_coverage_local": round(common / (b1 - b0), 4) if b1 > b0 else None}
    return chunks, stats


def compare_transcripts(ref: list[Line], loc: list[Line], room_patterns: list[str] | None = None) -> dict:
    wr = [(w.lower(), i) for i, (_, _, x) in enumerate(ref) for w in words_of(x)]
    wl = [(w.lower(), i) for i, (_, _, x) in enumerate(loc) for w in words_of(x)]
    sm = SequenceMatcher(None, [w for w, _ in wr], [w for w, _ in wl], autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= MIN_RUN]
    common = sum(b.size for b in blocks)
    by_local: dict[str, dict[str, int]] = {}
    for b in blocks:
        for k in range(b.size):
            ref_name = ref[wr[b.a + k][1]][1]
            loc_name = loc[wl[b.b + k][1]][1]
            by_local.setdefault(loc_name, {}).setdefault(ref_name, 0)
            by_local[loc_name][ref_name] += 1
    consistent = 0
    names_same = names_total = 0
    mapping: list[dict] = []
    for loc_name, counts in sorted(by_local.items(), key=lambda kv: -sum(kv[1].values())):
        ref_name, top = max(counts.items(), key=lambda kv: kv[1])
        total = sum(counts.values())
        consistent += top
        is_name = loc_name != "Me" and not re.fullmatch(r"Speaker \d+.*", loc_name) \
            and not is_room_like(ref_name, room_patterns)      # бот подписал переговоркой — не сравниваем
        same = same_person(loc_name, ref_name) if is_name else None
        if is_name:
            names_total += total
            names_same += total if same else 0
        mapping.append({"local": loc_name, "reference": ref_name, "share": round(top / total, 3),
                        "words": total, "same_person": same})
    only_ref, only_loc = _lost_passages(sm, wr, wl, ref, loc)
    smaller = min(len(wr), len(wl))
    return {
        "words_ref": len(wr), "words_local": len(wl), "lines_ref": len(ref), "lines_local": len(loc),
        "common": common,
        "coverage_ref": round(common / len(wr), 4) if wr else 0.0,
        "coverage_local": round(common / len(wl), 4) if wl else 0.0,
        "same_meeting": bool(smaller) and common / smaller >= MIN_COMMON,
        "speaker_consistency": round(consistent / common, 4) if common else None,
        "names_agree": round(names_same / names_total, 4) if names_total else None,
        "speakers": mapping,
        "only_reference": sorted(only_ref, key=lambda p: -p["words"])[:TOP_PASSAGES],
        "only_local": sorted(only_loc, key=lambda p: -p["words"])[:TOP_PASSAGES],
    }


# ---- прогон ---------------------------------------------------------------------


def reference_dir(cfg: Config) -> Path:
    return cfg.vault_root / cfg.reference_subdir


def judge_session(cfg: Config, client, ref: list[Line], loc: list[Line], session_id: str = "") -> dict:
    """Оценка контекста судьёй по отрезкам общего окна: сводка плюс отрезки с комментариями."""
    chunks, stats = overlap_chunks(ref, loc, cfg.judge_chunk_words)
    verdicts, details = [], []
    for k, (theirs, ours) in enumerate(chunks):
        v = judge_mod.judge_chunk(client, ours=ours, theirs=theirs)
        if v is None:
            details.append({"chunk": k, "error": "нет вердикта"})
            continue
        verdicts.append(v)
        details.append({"chunk": k, "ours": v.ours, "theirs": v.theirs, "better": v.better,
                        "missing_in_ours": v.missing_in_ours, "missing_in_theirs": v.missing_in_theirs,
                        "comment": v.comment, "order": v.order})
        log.info("judge %s #%d: мы %.0f/%.0f, бот %.0f/%.0f, лучше %s", session_id, k,
                 v.ours["understanding"], v.ours["coherence"], v.theirs["understanding"], v.theirs["coherence"], v.better)
    return {**stats, **judge_mod.summarize(verdicts), "details": details}


def evaluate_session(cfg: Config, session_dir: Path, client=None) -> dict | None:
    """Строка по сессии или None: нет Raw, приватная, или бот эту встречу не писал."""
    try:
        session = Session.load(session_dir)
    except Exception as e:  # noqa: BLE001
        log.warning("bench-ref: %s пропущена (%s)", session_dir.name, e)
        return None
    if session.meta.private or not session.state.raw_path or not Path(session.state.raw_path).exists():
        return None
    from .vault import title_of
    ref = find_reference(reference_dir(cfg), session.meta.started_at, title_of(session.meta))
    if ref is None:
        return None
    ref_lines = parse_reference(ref.read_text(encoding="utf-8"))
    loc_lines = parse_local(Path(session.state.raw_path).read_text(encoding="utf-8"))
    res = compare_transcripts(ref_lines, loc_lines, cfg.talk_room_patterns)
    row = {"id": session_dir.name, "reference": ref.name, **res}
    if client is not None and res["same_meeting"]:
        row["judge"] = judge_session(cfg, client, ref_lines, loc_lines, session_dir.name)
    return row


def aggregate(rows: list[dict]) -> dict:
    good = [r for r in rows if r["same_meeting"]]
    wr = sum(r["words_ref"] for r in good)
    wl = sum(r["words_local"] for r in good)
    common = sum(r["common"] for r in good)
    weights = [(r["speaker_consistency"], r["common"]) for r in good if r["speaker_consistency"] is not None]
    nm = [(r["names_agree"], r["common"]) for r in good if r["names_agree"] is not None]

    def wavg(pairs):
        tot = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / tot, 4) if tot else None

    return {"sessions": len(good), "unmatched": len(rows) - len(good), "words_ref": wr, "words_local": wl,
            "common": common, "coverage_ref": round(common / wr, 4) if wr else None,
            "coverage_local": round(common / wl, 4) if wl else None,
            "speaker_consistency": wavg(weights), "names_agree": wavg(nm)}


def aggregate_judge(rows: list[dict]) -> dict | None:
    judged = [r["judge"] for r in rows if r.get("judge") and r["judge"].get("chunks")]
    if not judged:
        return None
    n = sum(j["chunks"] for j in judged)

    def wmean(side, key):
        return round(sum(j[side][key] * j["chunks"] for j in judged) / n, 2)
    keys = ("understanding", "coherence", "garbled")
    return {"sessions": len(judged), "chunks": n,
            "ours": {k: wmean("ours", k) for k in keys}, "theirs": {k: wmean("theirs", k) for k in keys},
            "wins_ours": sum(j["wins_ours"] for j in judged), "wins_theirs": sum(j["wins_theirs"] for j in judged),
            "equal": sum(j["equal"] for j in judged),
            "tokens_in": sum(j.get("tokens_in", 0) for j in judged), "tokens_out": sum(j.get("tokens_out", 0) for j in judged)}


def run_bench_ref(cfg: Config, label: str, sessions: list[str] | None = None, since: str | None = None,
                  note: str | None = None, client=None) -> Path:
    """``client`` — судья контекста (см. judge.py); None — только текстовые метрики."""
    check_label(label)
    wanted = set(sessions or [])
    rows: list[dict] = []
    for d in sorted(p for p in cfg.sessions_dir.iterdir() if p.is_dir() and (p / "meta.json").exists()):
        if wanted and d.name not in wanted:
            continue
        if since and d.name[:10] < since:
            continue
        row = evaluate_session(cfg, d, client)
        if row:
            rows.append(row)
    if not rows:
        raise ValueError(f"bench-ref: ни одной сессии с заметкой бота в {reference_dir(cfg)}")
    totals = aggregate(rows)
    totals["judge"] = aggregate_judge(rows)
    runs_dir = cfg.bench_dir / "ref" / "runs"
    run = runs_dir / run_dir_name(next_run_number(runs_dir) if runs_dir.exists() else 0, label)
    run.mkdir(parents=True)
    (run / "metrics.json").write_text(json.dumps({"label": label, "totals": totals, "sessions": rows},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    (run / "report.md").write_text(format_report(rows), encoding="utf-8")
    (run / "config.yaml").write_text(yaml.safe_dump(flat_config(cfg), allow_unicode=True, sort_keys=True), encoding="utf-8")
    (run / "commit.txt").write_text(git_commit() + "\n", encoding="utf-8")
    if note:
        (run / "note.txt").write_text(note, encoding="utf-8")
    return run


# ---- таблицы и отчёт ----------------------------------------------------------------


def _pct(x) -> str:
    return EMPTY if x is None else f"{x * 100:.0f}%"


def session_rows(rows: list[dict]) -> str:
    cols = (("id", "сессия", "<"), ("words_ref", "слов бот", ">"), ("words_local", "слов мы", ">"),
            ("common", "общих", ">"), ("coverage_ref", "покр. бота", ">"), ("coverage_local", "покр. нас", ">"),
            ("speaker_consistency", "говорящие", ">"), ("names_agree", "имена", ">"), ("same_meeting", "", "<"))
    cells = []
    for r in rows:
        c = []
        for key, _, _ in cols:
            v = r.get(key)
            if key == "same_meeting":
                c.append("" if v else "≠ не та же речь")
            elif key in ("coverage_ref", "coverage_local", "speaker_consistency", "names_agree"):
                c.append(_pct(v))
            else:
                c.append(str(v) if v is not None else EMPTY)
        cells.append(c)
    widths = [max(len(t), *(len(c[i]) for c in cells)) for i, (_, t, _) in enumerate(cols)]
    out = ["  ".join(f"{t:{a}{w}}" for (_, t, a), w in zip(cols, widths))]
    out += ["  ".join(f"{c:{a}{w}}" for c, (_, _, a), w in zip(row, cols, widths)) for row in cells]
    return "\n".join(out)


def format_report(rows: list[dict]) -> str:
    out = ["# Сравнение с транскриптами бота Толка", "", session_rows(rows), ""]
    for r in rows:
        out += [f"## {r['id']} ← {r['reference']}", ""]
        if not r["same_meeting"]:
            out += ["Общих слов почти нет — бот писал другую встречу или мы другой звук.", ""]
            continue
        out += ["Говорящие (наша метка → ФИО у бота, доля общих слов):"]
        out += [f"- {m['local']} → {m['reference']} ({m['share'] * 100:.0f}% из {m['words']}"
                + (", другой человек" if m["same_person"] is False else "") + ")" for m in r["speakers"]]
        j = r.get("judge")
        if j and j.get("chunks"):
            out += ["", f"Судья контекста ({j['chunks']} отрезков общего окна; окно: бот {r['judge']['window_words_ref']} слов, "
                    f"мы {r['judge']['window_words_local']}, общих {r['judge']['window_common']}):",
                    f"- понятность: мы {j['ours']['understanding']}, бот {j['theirs']['understanding']}; "
                    f"связность: мы {j['ours']['coherence']}, бот {j['theirs']['coherence']}; "
                    f"мусор: мы {j['ours']['garbled']}, бот {j['theirs']['garbled']}; "
                    f"лучше: мы {j['wins_ours']}, бот {j['wins_theirs']}, поровну {j['equal']}"]
            for d in j["details"]:
                if "error" in d:
                    out.append(f"- отрезок {d['chunk']}: {d['error']}")
                    continue
                out.append(f"- отрезок {d['chunk']} ({d['better']}): {d['comment']}")
                out += [f"    · потеряно у нас: {x}" for x in d["missing_in_ours"][:5]]
                out += [f"    · потеряно у бота: {x}" for x in d["missing_in_theirs"][:5]]
        out += ["", "Есть только у бота (мы пропустили; время — по часам бота):"]
        out += [f"- {p['at']} · {p['words']} слов: {p['text']}" for p in r["only_reference"]] or [f"- {EMPTY}"]
        out += ["", "Есть только у нас (эхо, галлюцинация или бот пропустил; время — наше от старта записи):"]
        out += [f"- {p['at']} · {p['words']} слов: {p['text']}" for p in r["only_local"]] or [f"- {EMPTY}"]
        out.append("")
    return "\n".join(out)


def collect_rows(runs_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for d in run_dirs(runs_dir) if Path(runs_dir).exists() else []:
        f = d / "metrics.json"
        if not f.exists():
            continue
        t = json.loads(f.read_text(encoding="utf-8"))["totals"]
        j = t.get("judge") or {}
        row = {"label": d.name, **{k: v for k, v in t.items() if k != "judge"}}
        if j:
            row["judge_u"] = f"{j['ours']['understanding']}/{j['theirs']['understanding']}"
            row["judge_c"] = f"{j['ours']['coherence']}/{j['theirs']['coherence']}"
            row["judge_w"] = f"{j['wins_ours']}:{j['wins_theirs']}:{j['equal']}"
        rows.append(row)
    return rows


COLUMNS = (("label", "прогон", "<"), ("sessions", "встреч", ">"), ("unmatched", "мимо", ">"),
           ("words_ref", "слов бот", ">"), ("words_local", "слов мы", ">"), ("coverage_ref", "покр. бота", ">"),
           ("coverage_local", "покр. нас", ">"), ("speaker_consistency", "говорящие", ">"), ("names_agree", "имена", ">"),
           ("judge_u", "понятн. мы/бот", ">"), ("judge_c", "связн. мы/бот", ">"), ("judge_w", "лучше мы:бот:=", ">"))


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "прогонов пока нет"
    cells = [[_pct(r.get(k)) if k in ("coverage_ref", "coverage_local", "speaker_consistency", "names_agree")
              else (str(r.get(k)) if r.get(k) is not None else EMPTY) for k, _, _ in COLUMNS] for r in rows]
    widths = [max(len(t), *(len(c[i]) for c in cells)) for i, (_, t, _) in enumerate(COLUMNS)]
    out = ["  ".join(f"{t:{a}{w}}" for (_, t, a), w in zip(COLUMNS, widths))]
    out += ["  ".join(f"{c:{a}{w}}" for c, (_, _, a), w in zip(row, COLUMNS, widths)) for row in cells]
    return "\n".join(out)
