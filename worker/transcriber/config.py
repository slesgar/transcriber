from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .talk import BOT_PATTERNS as TALK_BOT_PATTERNS, ROOM_PATTERNS as TALK_ROOM_PATTERNS

log = logging.getLogger("transcriber")

# корень репозитория: отсюда берутся файлы-примеры и git sha для стенда
CODE_ROOT = Path(__file__).resolve().parents[2]

# каталог данных установки: config.yaml, app-config.json, glossary.txt, .env, sessions/, voices/, bench/,
# quality-log.md, quality-backlog.md. TRANSCRIBER_DATA нужен тестам и второй установке рядом с рабочей
DEFAULT_DATA_DIR = Path("~/Library/Application Support/Transcriber")
DATA_DIR = Path(os.environ.get("TRANSCRIBER_DATA") or DEFAULT_DATA_DIR).expanduser()   # пустая = не задана


@dataclass
class Config:
    vault_root: Path = Path("~/Documents/Vault").expanduser()
    private_dir: Path = Path("~/Documents/Transcriber Private").expanduser()   # приватные записи мимо вольта
    sessions_dir: Path = field(default_factory=lambda: DATA_DIR / "sessions")
    bench_dir: Path = field(default_factory=lambda: DATA_DIR / "bench")   # эталонные записи стенда качества и прогоны по ним
    # папки внутри вольта; раскладку конкретного вольта задаёт config.yaml установки
    raw_subdir: str = "Raw/Transcripts"
    meetings_subdir: str = "Meetings"
    reference_subdir: str = "Raw/TalkBot"   # транскрипты бота Толка — эталон стенда bench-ref
    # судья контекста для bench-ref --judge: модель Claude (ключ ANTHROPIC_API_KEY в .env каталога данных)
    judge_model: str = "claude-opus-5-5"
    judge_timeout_s: int = 300
    judge_chunk_words: int = 1200      # слов бота в одном отрезке для судьи
    sample_rate: int = 16000
    vad_threshold: float = 0.5
    min_silence_ms: int = 700
    speech_pad_ms: int = 100
    max_utterance_s: float = 30.0
    block_min_s: float = 45.0        # короче блок не закрываем: whisper любит окно ~30 с и контекст в нём
    block_max_s: float = 90.0        # длиннее не копим, иначе текст ждёт слишком долго
    block_gap_s: float = 1.5         # пауза, по которой блок режется, когда минимум набран
    block_gap_keep_s: float = 0.5    # сколько тишины оставляем в склейке от реальной паузы
    block_idle_s: float = 4.0        # тишина после последней реплики — закрываем блок, не ждём
    carry_context: bool = True       # хвост прошлого блока канала уходит в промпт следующего
    read_chunk_s: float = 30.0      # сколько аудио за один step(), чтобы бэклог не влезал в RAM целиком
    min_embed_s: float = 2.0         # короче отпечаток шумный: реплика наследует метку соседа (стенд 14.10)
    speaker_similarity: float = 0.4  # 0.3 чаще склеивал людей; выше 0.45 дробит одного человека (стенд 14.10)
    voices_dir: Path = field(default_factory=lambda: DATA_DIR / "voices")   # библиотека голосовых профилей, по файлу на человека
    voice_match_similarity: float = 0.45        # ниже — кластер остаётся «Speaker N»: ошибиться именем хуже
    voice_min_samples: int = 3                  # кластер короче — имени не даём, отпечаток ненадёжен
    # Плитки участников из интерфейса Толка (talk.jsonl рядом с дорожками сессии)
    talk_room_patterns: list[str] = field(default_factory=lambda: list(TALK_ROOM_PATTERNS))
    talk_bot_patterns: list[str] = field(default_factory=lambda: list(TALK_BOT_PATTERNS))
    talk_shared_names: list[str] = field(default_factory=list)   # общие микрофоны и владелец: имён из Толка не получают
    talk_split_share: float = 0.3   # кластер с двумя именами Толка от такой доли каждое режется по плиткам; 0 — выкл.
    talk_split_min_count: int = 3   # ...и не меньше стольких названных реплик у каждого: одна чужая плитка — не склейка
    # владелец установки: имя в заметках и библиотеке голосов, подпись его плитки в Толке, заголовок
    # его секции в MTG-заглушке (вольт ищет его дословно); автообучение голоса — см. owner.py
    owner_name: str = "Owner"
    owner_talk_name: str = ""
    owner_thoughts_heading: str = "## Мысли владельца"
    owner_autolearn: bool = True
    owner_min_samples: int = 30
    talk_autolearn: bool = True                 # уверенно названный Толком кластер сам идёт в библиотеку голосов
    echo_overlap: float = 0.8
    echo_similarity: float = 0.6
    echo_strong_similarity: float = 0.8   # дословный повтор — эхо даже при коротком перекрытии
    echo_wait_s: float = 1.0
    echo_suppress: bool = False   # вычитать эхо динамиков из микрофона по дорожке sys (aec.py)
    echo_beta: float = 2.0        # во сколько раз смелее вычитаем: запас на недооценку тракта
    echo_beta_loud: float = 0.0   # запас при громких динамиках (усиление ≥ 0.4), линейно от echo_beta; 0 — выкл.: стенд 14.10 выигрыша не показал
    echo_floor: float = 0.05      # ниже этого коэффициента корзину не опускаем — останется дыра
    echo_kept_max: float = 0.15   # реплика микрофона, от которой подавитель оставил меньше, — остаток эха; 0 — выкл.
    echo_kept_min_cover: float = 0.8   # ...если она хотя бы настолько накрыта речью динамиков
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    embedding_model: str = "pyannote/wespeaker-voxceleb-resnet34-LM"
    mlx_cache_mb: int = 2048     # потолок кеша Metal-буферов MLX; без него whisper за сутки съедал всю память (asr.py)
    idle_restart_mb: int = 3500  # встреч нет, а воркер держит больше — выходит, launchd поднимает чистый; 0 — выкл.
    busy_restart_mb: int = 6000  # то же посреди встречи: сессия продолжится с сохранённых смещений; 0 — выкл.
    abandoned_after_min: int = 10   # запись без STOP, чьё аудио столько не растёт, — брошена: финализируем
    continue_gap_min: int = 20      # сессия с тем же названием через столько минут после конца прошлой — её продолжение; 0 — выкл.
    language: str = "ru"
    hallucinations: list[str] = field(default_factory=list)
    glossary: list[str] = field(default_factory=list)
    keep_audio: bool = False
    marker_every_s: int = 300

    def __post_init__(self) -> None:
        # плитка владельца в Толке не даёт имён — как общий микрофон; в YAML её отдельно не дублируют
        if self.owner_talk_name and self.owner_talk_name not in self.talk_shared_names:
            self.talk_shared_names = [*self.talk_shared_names, self.owner_talk_name]


def load_config(yaml_path: Path | None = None, glossary_path: Path | None = None) -> Config:
    load_dotenv(DATA_DIR / ".env")
    yaml_path = yaml_path or DATA_DIR / "config.yaml"
    glossary_path = glossary_path or DATA_DIR / "glossary.txt"
    data = yaml.safe_load(yaml_path.read_text()) if yaml_path.exists() else {}
    data = data or {}
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        log.warning("config.yaml: unknown keys ignored: %s", ", ".join(unknown))
    kwargs = {k: v for k, v in data.items() if k in known}
    for key in ("vault_root", "private_dir", "sessions_dir", "voices_dir", "bench_dir"):
        if key in kwargs:
            kwargs[key] = Path(kwargs[key]).expanduser()
    if glossary_path.exists():
        kwargs["glossary"] = [line.strip() for line in glossary_path.read_text().splitlines() if line.strip()]
    return Config(**kwargs)
