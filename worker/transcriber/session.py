from __future__ import annotations

import json
import logging
import os

import numpy as np
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

log = logging.getLogger("transcriber")


@dataclass
class Meta:
    id: str
    started_at: datetime
    app: str = "manual"
    title: str = ""
    participants: list[str] = field(default_factory=list)
    private: bool = False      # приложение пометило запись приватной: приватная запись в рабочий вольт не идёт

    @classmethod
    def from_dict(cls, d: dict) -> "Meta":
        started = datetime.fromisoformat(d["started_at"])
        if started.tzinfo is None:                 # writer omitted the offset — treat as local time
            started = started.astimezone()
        return cls(id=d["id"], started_at=started, app=d.get("app", "manual"),
                   title=d.get("title", "") or "", participants=list(d.get("participants") or []),
                   private=bool(d.get("private", False)))


@dataclass
class Rec:
    """One transcribed utterance. text=None means ASR failed on it."""
    channel: str          # "mic" | "sys"
    start: float          # seconds from session start
    end: float
    text: str | None
    speaker: str          # "Me" | "Speaker N"
    emb: int | None = None  # index into State.embeddings (sys) or State.mic_embeddings (mic)
    kept: float | None = None  # доля энергии микрофона, оставленная подавителем эха; None — не считали
    talk: str | None = None    # имя из плиток Толка на этот отрезок; None — Толк молчит или это переговорка

    @classmethod
    def from_dict(cls, d: dict) -> "Rec":
        return cls(**_known(cls, d))


@dataclass
class State:
    status: str = "recording"      # recording | finalized
    mic_offset: int = 0            # samples already segmented (safe resume point)
    sys_offset: int = 0
    raw_path: str | None = None
    note_path: str | None = None
    records: list[Rec] = field(default_factory=list)
    pending_mic: list[Rec] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    speaker_of_emb: list[int] = field(default_factory=list)
    # Микрофон — отдельное пространство индексов: у mic-реплики Rec.emb указывает сюда, а не в embeddings,
    # чтобы голоса гостей на общем микрофоне не смешивались с кластерами собеседников из sys.
    mic_embeddings: list[list[float]] = field(default_factory=list)
    mic_cluster_of_emb: list[int] = field(default_factory=list)
    mic_speaker_names: dict[str, str] = field(default_factory=dict)   # кластер микрофона (строкой) → имя
    speaker_names: dict[str, str] = field(default_factory=dict)   # индекс кластера (строкой) → имя из библиотеки голосов
    errors: list[str] = field(default_factory=list)
    continues: str | None = None      # id предыдущей части той же встречи (continuation.py)
    continued_by: str | None = None   # id следующей части

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        s = cls(**{k: v for k, v in _known(cls, d).items() if k not in ("records", "pending_mic")})
        s.records = [Rec.from_dict(r) for r in d.get("records") or []]
        s.pending_mic = [Rec.from_dict(r) for r in d.get("pending_mic") or []]
        return s


def _known(cls, d: dict) -> dict:
    """Ignore keys a newer (or older) version wrote — state.json must never block a restart."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


class Session:
    def __init__(self, path: Path, meta: Meta, state: State):
        self.dir = Path(path)
        self.meta = meta
        self.state = state

    @classmethod
    def load(cls, path: Path) -> "Session":
        path = Path(path)
        meta = Meta.from_dict(json.loads((path / "meta.json").read_text()))
        state_file = path / "state.json"
        state = State.from_dict(json.loads(state_file.read_text())) if state_file.exists() else State()
        # Эмбеддинги — в бинарном сайдкаре (тикет 10); в старых сессиях они ещё внутри state.json,
        # и тогда сайдкар не читаем: JSON старше и полнее
        if state_file.exists() and not state.embeddings and not state.mic_embeddings:
            state.embeddings, state.mic_embeddings = load_embeddings(path / EMBEDDINGS_FILE)
            reconcile_embeddings(state, path.name)
        return cls(path, meta, state)

    @property
    def mic_pcm(self) -> Path:
        return self.dir / "mic.pcm"

    @property
    def sys_pcm(self) -> Path:
        return self.dir / "sys.pcm"

    def stop_requested(self) -> bool:
        return (self.dir / "STOP").exists()

    def save_state(self) -> None:
        """state.json без массивов эмбеддингов, сами массивы — в ``embeddings.npz`` рядом.

        Эмбеддинг на каждую реплику обоих каналов в JSON с отступами давал ~18 МБ и 0,4 с на шаг
        к концу двухчасовой встречи, а переписывается файл каждый шаг конвейера (тикет 10).
        Ключи ``embeddings``/``mic_embeddings`` остаются в JSON пустыми — старый читатель не ломается.
        """
        st = self.state
        sys_embs, mic_embs = st.embeddings, st.mic_embeddings
        st.embeddings, st.mic_embeddings = [], []
        try:
            payload = asdict(st)
        finally:
            st.embeddings, st.mic_embeddings = sys_embs, mic_embs
        save_embeddings(self.dir / EMBEDDINGS_FILE, sys_embs, mic_embs)
        atomic_write_text(self.dir / "state.json", json.dumps(payload, ensure_ascii=False, indent=1))


EMBEDDINGS_FILE = "embeddings.npz"


def save_embeddings(path: Path, sys_embs: list[list[float]], mic_embs: list[list[float]]) -> None:
    """Оба массива float32 в один несжатый npz: ~1 КБ на реплику вместо ~9 КБ текста; запись атомарная."""
    path = Path(path)
    for stale in path.parent.glob(f"{path.name}.*.tmp.npz"):     # хвосты от упавшего процесса
        stale.unlink(missing_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp.npz")
    def matrix(embs: list[list[float]]) -> np.ndarray:
        return np.asarray(embs, dtype=np.float32) if embs else np.zeros((0, 0), dtype=np.float32)
    with open(tmp, "wb") as f:
        np.savez(f, sys=matrix(sys_embs), mic=matrix(mic_embs))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def reconcile_embeddings(st: State, session_id: str = "") -> None:
    """Сайдкар и state.json пишутся двумя файлами: крах между ними оставляет массивы и метки кластеров
    разной длины. Лишний хвост массива (он новее) отбрасываем; если массив короче меток — режем метки
    и снимаем ссылки реплик за край: реплика без эмбеддинга — штатный случай, кривой индекс — нет."""
    for arr_name, lab_name, channel in (("embeddings", "speaker_of_emb", "sys"),
                                        ("mic_embeddings", "mic_cluster_of_emb", "mic")):
        arr, labels = getattr(st, arr_name), getattr(st, lab_name)
        if len(arr) == len(labels):
            continue
        n = min(len(arr), len(labels))
        log.warning("%s: %s %d и %s %d разной длины — оставляем %d", session_id, arr_name, len(arr),
                    lab_name, len(labels), n)
        setattr(st, arr_name, arr[:n])
        setattr(st, lab_name, labels[:n])
        for r in (*st.records, *st.pending_mic):
            if r.channel == channel and r.emb is not None and r.emb >= n:
                r.emb = None


def load_embeddings(path: Path) -> tuple[list[list[float]], list[list[float]]]:
    """Массивы из сайдкара; нет файла или он битый — пустые списки (реплики без эмбеддингов, не падение)."""
    path = Path(path)
    if not path.exists():
        return [], []
    try:
        with np.load(path) as data:
            return [list(map(float, row)) for row in data["sys"]], [list(map(float, row)) for row in data["mic"]]
    except (OSError, KeyError, ValueError) as e:
        log.warning("%s: сайдкар эмбеддингов не читается (%s) — реплики остаются без эмбеддингов", path, e)
        return [], []


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")   # another process must not clobber our tmp
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())                                 # without fsync a power loss leaves an empty file
    os.replace(tmp, path)


class SessionLocked(RuntimeError):
    pass


def lock_holder(dir: Path) -> int | None:
    """PID живого процесса, держащего сессию (кроме нас самих), иначе None. Мёртвый замок не считается."""
    f = Path(dir) / "LOCK"
    try:
        pid = int(f.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


class session_lock:
    """`with session_lock(dir):` — эксклюзивная работа над сессией из CLI (retranscribe, refinalize,
    finalize). Демон такие сессии пропускает, иначе два процесса пишут в один state.json."""

    def __init__(self, dir: Path):
        self.file = Path(dir) / "LOCK"
        self.dir = Path(dir)

    def __enter__(self):
        holder = lock_holder(self.dir)
        if holder is not None:
            raise SessionLocked(f"сессию {self.dir.name} сейчас обрабатывает процесс {holder}")
        self.file.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        self.file.unlink(missing_ok=True)
        return False
