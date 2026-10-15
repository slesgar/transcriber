"""Стенд качества: один и тот же разговор, прогнанный разными версиями кода.

Эталонная запись лежит в ``bench/<bench_id>/`` (``mic.pcm``, ``sys.pcm``, ``meta.json``) и не
меняется никогда; каждый прогон кладёт рядом ``runs/NN-<label>/`` — транскрипт, метрики,
действующий конфиг, коммит и лог. Эталонного транскрипта у нас нет, поэтому метрики
описательные: они не говорят «стало лучше», они показывают, что изменилось, а решает человек.

Прогон идёт во временном каталоге сессии и временном вольте: рабочий вольт, рабочие сессии и
записи встреч не трогаются. Библиотека голосов только читается — именами кластеров; ``learn``
стенд не вызывает, иначе эталонная запись отравила бы профили.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, fields, replace
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from .config import Config, CODE_ROOT
from .models import Models, load_models
from .pipeline import Pipeline
from .session import Rec, Session

log = logging.getLogger("transcriber")

BODY_MARK = "## Полный транскрипт"
# слово начинается с буквы: «06:10» из «[не распознано 06:10–06:14]» словом не считается
WORD_RE = re.compile(r"[^\W\d_][\w-]*", re.UNICODE)
MARKER_RE = re.compile(r"^—\s*(\d{2}:\d{2})\s*—$")
LINE_RE = re.compile(r"^(.{1,60}?):\s(.*)$")
SPEAKER_N_RE = re.compile(r"^Speaker \d+$")
# то же, что ловят фильтры quality.py, но уже в готовом транскрипте: ярлык промпта, ставший репликой
JUNK_RE = re.compile(r"(?:Термин|Терминатор|Участник)\w*\s*[:,.…]")
MIC = "Me"
START_MARKER = "00:00"

RUN_RE = re.compile(r"^(\d{2})-")
LABEL_RE = re.compile(r"^[^\W_][\w-]*$", re.UNICODE)

MIN_PASSAGE_WORDS = 12      # короче — обычное разночтение модели, а не пропавший кусок разговора
TOP_PASSAGES = 10
PASSAGE_PREVIEW = 20        # слов в превью куска: хватает узнать место, не хватает утопить отчёт


# ---- метрики из готового транскрипта ------------------------------------------


def transcript_body(raw_text: str) -> str:
    """Тело транскрипта без фронт-маттера и шапки — считаем только реплики."""
    return raw_text.split(BODY_MARK, 1)[1] if BODY_MARK in raw_text else raw_text


def utterances(raw_text: str) -> list[tuple[str, str, str]]:
    """Реплики транскрипта: (ближайший маркер времени сверху, говорящий, текст)."""
    out: list[tuple[str, str, str]] = []
    marker = START_MARKER
    for line in transcript_body(raw_text).splitlines():
        line = line.strip()
        if not line:
            continue
        m = MARKER_RE.match(line)
        if m:
            marker = m.group(1)
            continue
        m = LINE_RE.match(line)
        if m:
            out.append((marker, m.group(1).strip(), m.group(2).strip()))
    return out


def words_of(text: str) -> list[str]:
    return WORD_RE.findall(text or "")


def percentile(values: list[int], q: float) -> float:
    """Ближайший ранг: p95 маленькой выборки — это просто длинный хвост, без интерполяций."""
    if not values:
        return 0.0
    s = sorted(values)
    return float(s[max(0, math.ceil(q * len(s)) - 1)])


def transcript_metrics(raw_text: str) -> dict:
    """Всё, что видно по одному raw.md — без аудио, конфига и лога.

    Считается и для прогонов, сделанных до появления стенда: у них есть только транскрипт.
    """
    lines = utterances(raw_text)
    per_line = [len(words_of(text)) for _, _, text in lines]
    per_speaker: dict[str, int] = {}
    mic = sys_ = 0
    for (_, speaker, text), n in zip(lines, per_line):
        per_speaker[speaker] = per_speaker.get(speaker, 0) + 1
        if speaker == MIC:
            mic += n
        else:
            sys_ += n
    others = [s for s in per_speaker if s != MIC]
    return {
        "lines": len(lines),
        "words": mic + sys_,
        "words_mic": mic,
        "words_sys": sys_,
        "speakers": len(others),
        "lines_per_speaker": {k: per_speaker[k] for k in sorted(per_speaker)},
        "identified": sorted(s for s in others if not SPEAKER_N_RE.match(s)),
        "junk_lines": sum(1 for _, _, text in lines if JUNK_RE.search(text)),
        "words_per_line_mean": round(sum(per_line) / len(per_line), 2) if per_line else 0.0,
        "words_per_line_p95": percentile(per_line, 0.95),
    }


# ---- метрики прогона ----------------------------------------------------------


LOG_RULES: dict[str, re.Pattern] = {
    "prompt leak": re.compile(r"dropped \(prompt leak\)"),
    "repetitive": re.compile(r"dropped \(repetitive\)"),
    "foreign": re.compile(r"dropped \(foreign\)"),
    "filler": re.compile(r"dropped \(filler\)"),
    "echo dropped": re.compile(r"echo dropped"),
    "echo residue (acoustic)": re.compile(r"echo residue \(acoustic\) dropped"),
    "retry without prompt": re.compile(r"retry without prompt"),
    "asr failed": re.compile(r"asr failed"),
}
# выброшенная речь: ретрай без промпта и падение ASR речь не отнимают, в общий счёт не идут
DROP_RULES = ("prompt leak", "repetitive", "foreign", "filler", "echo dropped", "echo residue (acoustic)")


def count_log_events(lines: list[str]) -> dict[str, int]:
    return {name: sum(1 for line in lines if rx.search(line)) for name, rx in LOG_RULES.items()}


def drops(events: dict[str, int]) -> int:
    return sum(events.get(name, 0) for name in DROP_RULES)


def blanked_seconds(records: list[Rec]) -> float:
    """Секунды речи, которые фильтры обнулили: текст пустой, а время реплики осталось.

    ``text is None`` — это падение ASR, там речи не было отнято, только не распознано.
    """
    return round(sum(r.end - r.start for r in records if r.text == ""), 2)


class LogCapture(logging.Handler):
    """Лог воркера за время прогона — в память, чтобы лечь рядом с транскриптом."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def __enter__(self) -> "LogCapture":
        log.addHandler(self)
        self._level = log.level
        if not log.isEnabledFor(logging.INFO):
            log.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc) -> bool:
        log.removeHandler(self)
        log.setLevel(self._level)
        return False


# ---- нумерация прогонов, метка, --set -----------------------------------------


def next_run_number(runs_dir: Path) -> int:
    """Следующий номер — от максимума, а не от количества: дыры в нумерации не переиспользуем."""
    used = [int(m.group(1)) for p in Path(runs_dir).glob("*")
            if p.is_dir() and (m := RUN_RE.match(p.name))]
    return max(used) + 1 if used else 0


def run_dir_name(number: int, label: str) -> str:
    return f"{number:02d}-{label}"


def check_label(label: str) -> str:
    if not LABEL_RE.match(label or ""):
        raise ValueError(f"{label!r}: метка прогона — одно слово из букв, цифр, дефисов "
                         "и подчёркиваний (например current-main)")
    return label


def _cast(key: str, value: str, current):
    if isinstance(current, bool):
        if value.lower() in ("1", "true", "yes", "on", "да"):
            return True
        if value.lower() in ("0", "false", "no", "off", "нет"):
            return False
        raise ValueError(f"{key}={value!r}: нужно true или false")
    if isinstance(current, Path):
        return Path(value).expanduser()
    if isinstance(current, int):
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{key}={value!r}: нужно целое число") from None
    if isinstance(current, float):
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"{key}={value!r}: нужно число") from None
    if isinstance(current, str):
        return value
    raise ValueError(f"{key}: значение такого типа через --set не задаётся — правь config.yaml")


def parse_overrides(pairs: list[str], cfg: Config) -> dict:
    """«block_min_s=30» → {'block_min_s': 30.0}. Тип берём у текущего значения поля."""
    known = {f.name for f in fields(Config)}
    out: dict = {}
    for text in pairs or []:
        key, sep, value = text.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"{text!r}: нужна пара вида key=value — со знаком =")
        if key not in known:
            near = [k for k in sorted(known) if k.startswith(key[:6])]
            raise ValueError(f"{key!r}: такого поля в конфиге нет"
                             + (f"; похоже на {', '.join(near)}" if near else ""))
        out[key] = _cast(key, value.strip(), getattr(cfg, key))
    return out


# ---- прогон -------------------------------------------------------------------


def git_commit() -> str:
    """Короткий SHA плюс отметка о незакоммиченных правках — чтобы прогон был воспроизводим."""
    try:
        sha = subprocess.run(["git", "-C", str(CODE_ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(CODE_ROOT), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "git недоступен"
    return f"{sha} (есть незакоммиченные правки)" if dirty else sha


def flat_config(cfg: Config) -> dict:
    return {k: str(v) if isinstance(v, Path) else v for k, v in asdict(cfg).items()}


def audio_seconds(bench_dir: Path, sample_rate: int) -> float:
    """Длина эталона по размеру дорожек: int16 моно, без заголовков."""
    sizes = [p.stat().st_size for p in (bench_dir / "mic.pcm", bench_dir / "sys.pcm") if p.exists()]
    return round(max(sizes, default=0) / 2 / sample_rate, 2)


def run_bench(cfg: Config, bench_id: str, label: str, overrides: dict | None = None,
              note: str | None = None, models: Models | None = None) -> Path:
    """Прогнать эталонную запись текущим кодом и положить результат в новый каталог прогона."""
    check_label(label)
    src = cfg.bench_dir / bench_id
    if not (src / "meta.json").exists():
        raise ValueError(f"{src}: эталонной записи с таким id нет")
    if not (src / "mic.pcm").exists() and not (src / "sys.pcm").exists():
        raise ValueError(f"{src}: нет ни mic.pcm, ни sys.pcm")

    sandbox = Path(tempfile.mkdtemp(prefix=f"bench-{bench_id}-"))
    session_dir = sandbox / "sessions" / bench_id
    try:
        session_dir.mkdir(parents=True)
        for name in ("meta.json", "mic.pcm", "sys.pcm", "talk.jsonl"):
            if (src / name).exists():
                shutil.copy2(src / name, session_dir / name)   # эталон только читаем, работаем с копией
        (session_dir / "STOP").touch()
        # песочница поверх --set: перенаправить прогон в рабочий вольт нельзя даже нарочно,
        # keep_audio — чтобы finalize() не снёс копию эталона посреди прогона, talk_autolearn
        # выключен — эталон гоняется десятки раз и не должен пополнять библиотеку голосов; owner_autolearn
        # тоже, а voices_dir — пустой каталог песочницы: метки микрофона не должны зависеть от того,
        # какой профиль владельца накопился в реальной библиотеке к моменту прогона
        run_cfg = replace(cfg, **(overrides or {}))
        run_cfg = replace(run_cfg, vault_root=sandbox / "vault", sessions_dir=sandbox / "sessions",
                          private_dir=sandbox / "private", bench_dir=cfg.bench_dir, keep_audio=True,
                          talk_autolearn=False, owner_autolearn=False,
                          voices_dir=sandbox / "voices")

        m = models or load_models(run_cfg)
        with LogCapture() as captured:
            started = time.monotonic()
            pipe = Pipeline(run_cfg, Session.load(session_dir), m.asr, m.embed, m.prob_factory)
            while pipe.step() or pipe.has_backlog():
                pass
            pipe.finalize()
            wall = time.monotonic() - started

        st = pipe.s.state
        raw_text = Path(st.raw_path).read_text(encoding="utf-8")
        seconds = audio_seconds(src, run_cfg.sample_rate)
        metrics = {
            "label": label,
            "bench_id": bench_id,
            "audio_minutes": round(seconds / 60, 2),
            "wall_seconds": round(wall, 3),            # без загрузки моделей: она общая для всех прогонов
            "realtime_factor": round(seconds / wall, 2) if wall else 0.0,   # >1 — быстрее реального времени
            **transcript_metrics(raw_text),
            "clusters": len(set(st.speaker_of_emb)),
            "blanked_seconds": blanked_seconds(st.records),
            "log_events": count_log_events(captured.lines),
            "asr_errors": len(st.errors),
        }

        run = src / "runs" / run_dir_name(next_run_number(src / "runs"), label)
        run.mkdir(parents=True)
        (run / "raw.md").write_text(raw_text, encoding="utf-8")
        (run / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
        (run / "config.yaml").write_text(
            yaml.safe_dump(flat_config(run_cfg), allow_unicode=True, sort_keys=True), encoding="utf-8")
        (run / "commit.txt").write_text(git_commit() + "\n", encoding="utf-8")
        (run / "worker.log").write_text("\n".join(captured.lines) + "\n", encoding="utf-8")
        if note:
            (run / "note.txt").write_text(note, encoding="utf-8")
        return run
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


# ---- таблица прогонов ---------------------------------------------------------


def run_dirs(runs_dir: Path) -> list[Path]:
    return sorted((p for p in Path(runs_dir).glob("*") if p.is_dir() and RUN_RE.match(p.name)),
                  key=lambda p: p.name)


def collect_rows(runs_dir: Path) -> list[dict]:
    """Строка на каждый прогон. У засеянных прогонов метрик нет — считаем по их raw.md."""
    rows: list[dict] = []
    for d in run_dirs(runs_dir):
        metrics_file, raw_file = d / "metrics.json", d / "raw.md"
        if metrics_file.exists():
            m = json.loads(metrics_file.read_text(encoding="utf-8"))
            timed = True
        elif raw_file.exists():
            m, timed = transcript_metrics(raw_file.read_text(encoding="utf-8")), False
        else:
            continue
        rows.append({
            "label": d.name,
            "words": m.get("words"),
            "lines": m.get("lines"),
            "speakers": m.get("speakers"),
            "junk": m.get("junk_lines"),
            "blanked": m.get("blanked_seconds") if timed else None,
            "drops": drops(m.get("log_events") or {}) if timed else None,
            "rtf": m.get("realtime_factor") if timed else None,
        })
    return rows


COLUMNS = (("label", "прогон", "<"), ("words", "слов", ">"), ("lines", "реплик", ">"),
           ("speakers", "спикеров", ">"), ("junk", "мусор", ">"), ("blanked", "глушено с", ">"),
           ("drops", "дропов", ">"), ("rtf", "×реалтайм", ">"))
EMPTY = "—"


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "прогонов пока нет"
    cells = [[str(r.get(key)) if r.get(key) is not None else EMPTY for key, _, _ in COLUMNS] for r in rows]
    widths = [max(len(title), *(len(c[i]) for c in cells)) for i, (_, title, _) in enumerate(COLUMNS)]
    out = ["  ".join(f"{title:{align}{w}}" for (_, title, align), w in zip(COLUMNS, widths))]
    out += ["  ".join(f"{c:{align}{w}}" for c, (_, _, align), w in zip(row, COLUMNS, widths)) for row in cells]
    return "\n".join(out)


# ---- сравнение двух прогонов --------------------------------------------------


def find_run(runs_dir: Path, key: str) -> Path:
    """Прогон по имени каталога, по номеру («01») или по метке («prompt-and-filters»)."""
    dirs = run_dirs(runs_dir)
    for d in dirs:
        if d.name == key:
            return d
    hits = [d for d in dirs if d.name.startswith(f"{key}-") or d.name.split("-", 1)[-1] == key]
    if len(hits) == 1:
        return hits[0]
    known = ", ".join(d.name for d in dirs) or EMPTY
    if not hits:
        raise ValueError(f"{key!r}: такого прогона нет; есть {known}")
    raise ValueError(f"{key!r}: подходит больше одного прогона — {', '.join(d.name for d in hits)}")


def normalized_words(raw_text: str) -> list[tuple[str, str]]:
    """Слова транскрипта в нижнем регистре без пунктуации; рядом — маркер времени сверху.

    Ярлыки говорящих и маркеры выброшены: сравниваем разговор, а не разметку.
    """
    out: list[tuple[str, str]] = []
    for marker, _speaker, text in utterances(raw_text):
        out += [(w.lower(), marker) for w in words_of(text)]
    return out


def _passage(items: list[tuple[str, str]], i1: int, i2: int) -> dict:
    chunk = items[i1:i2]
    return {"at": chunk[0][1], "words": len(chunk),
            "text": " ".join(w for w, _ in chunk[:PASSAGE_PREVIEW])
                    + (" …" if len(chunk) > PASSAGE_PREVIEW else "")}


def compare(a_text: str, b_text: str, min_words: int = MIN_PASSAGE_WORDS,
            top: int = TOP_PASSAGES) -> dict:
    """Пословное сравнение двух транскриптов: доля общего и самые длинные расхождения."""
    a, b = normalized_words(a_text), normalized_words(b_text)
    aw, bw = [w for w, _ in a], [w for w, _ in b]
    sm = SequenceMatcher(None, aw, bw, autojunk=False)
    matched = sum(block.size for block in sm.get_matching_blocks())
    only_a: list[dict] = []
    only_b: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace") and i2 - i1 >= min_words:
            only_a.append(_passage(a, i1, i2))
        if tag in ("insert", "replace") and j2 - j1 >= min_words:
            only_b.append(_passage(b, j1, j2))
    order = lambda xs: sorted(xs, key=lambda p: -p["words"])[:top]   # noqa: E731
    return {
        "words_a": len(aw), "words_b": len(bw), "matched": matched,
        "share_a": round(matched / len(aw), 4) if aw else 0.0,
        "share_b": round(matched / len(bw), 4) if bw else 0.0,
        "only_a": order(only_a), "only_b": order(only_b),
    }


def format_diff(res: dict, name_a: str, name_b: str) -> str:
    out = [f"{name_a}: {res['words_a']} слов · {name_b}: {res['words_b']} слов · общих {res['matched']}",
           f"из {name_a} нашлось в {name_b}: {res['share_a'] * 100:.1f} %",
           f"из {name_b} нашлось в {name_a}: {res['share_b'] * 100:.1f} %"]
    for key, title in (("only_a", f"Только в {name_a}"), ("only_b", f"Только в {name_b}")):
        out += ["", f"{title} (кусков от {MIN_PASSAGE_WORDS} слов: {len(res[key])})"]
        out += [f"  {p['at']} · {p['words']} слов: {p['text']}" for p in res[key]] or [f"  {EMPTY}"]
    return "\n".join(out)


def diff_runs(cfg: Config, bench_id: str, key_a: str, key_b: str) -> str:
    runs_dir = cfg.bench_dir / bench_id / "runs"
    if not runs_dir.exists():
        raise ValueError(f"{runs_dir}: прогонов по этой записи ещё нет")
    a, b = find_run(runs_dir, key_a), find_run(runs_dir, key_b)
    for d in (a, b):
        if not (d / "raw.md").exists():
            raise ValueError(f"{d.name}: в прогоне нет raw.md")
    res = compare((a / "raw.md").read_text(encoding="utf-8"), (b / "raw.md").read_text(encoding="utf-8"))
    return format_diff(res, a.name, b.name)
