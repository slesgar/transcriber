"""Первичная настройка: ``transcriber init`` создаёт каталог данных из примеров и спрашивает минимум.

Каталог данных (``config.DATA_DIR``) живёт вне репозитория; здесь он собирается из файлов-примеров
репозитория. Повторный запуск существующие файлы не трогает и ответы к ним не применяет.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import CODE_ROOT

EXAMPLES: dict[str, Path] = {
    "config.yaml": CODE_ROOT / "worker" / "config.example.yaml",
    "app-config.json": CODE_ROOT / "app-config.example.json",
    "glossary.txt": CODE_ROOT / "glossary.example.txt",
}
SUBDIRS = ("sessions", "voices", "bench")

QUALITY_LOG_HEADER = """# Журнал качества

Сюда владелец записывает, что пошло не так в расшифровках: по строке на замеченный симптом.
Разбирается по этому журналу, когда накопится.

Формат: раздел на день, в нём строки с временем.

```
## DD.MM.YYYY (прогон DD.MM)

- HH:MM — <симптом>
```

Правило: дата, время, симптом — без имён участников и тем встреч. Симптом описывает поведение
расшифровки («реплика обрезана», «два голоса слиты в один»), а не содержание разговора.
"""


@dataclass
class Answers:
    owner_name: str = "Owner"
    owner_talk_name: str = ""
    vault_root: str = "~/Documents/Vault"
    private_dir: str = "~/Documents/Transcriber Private"
    meeting_hosts: list[str] = field(default_factory=list)


def ask(defaults: Answers, read: Callable[[str], str] = input,
        write: Callable[[str], None] = print) -> Answers:
    """Пять вопросов по-русски; пустой ввод оставляет умолчание в скобках."""
    write("Первичная настройка. Пустой ответ оставляет значение в скобках.")

    def one(question: str, default: str) -> str:
        return read(f"{question} [{default}]: ").strip() or default

    owner = one("Как зовут владельца (имя в заметках)", defaults.owner_name)
    talk = one("Имя владельца в Толке, как в сетке участников", defaults.owner_talk_name)
    vault = one("Папка вольта Obsidian", defaults.vault_root)
    private = one("Папка приватных записей", defaults.private_dir)
    hosts_default = ", ".join(defaults.meeting_hosts)
    hosts_raw = one("Хосты рабочих звонков в браузере через запятую", hosts_default)
    hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
    return Answers(owner, talk, vault, private, hosts)


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    return str(value)


def set_yaml_keys(text: str, values: dict[str, object]) -> str:
    """Заменить строки ``key: …`` верхнего уровня, остальное (комментарии, порядок) оставить на месте.

    Ключа нет — дописывается в конец.
    """
    for key, value in values.items():
        line = f"{key}: {_scalar(value)}"
        text, n = re.subn(rf"^{re.escape(key)}:.*$", lambda _m: line, text, count=1, flags=re.M)
        if not n:
            text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    return text


def set_privacy_hosts(text: str, hosts: list[str]) -> str:
    obj = json.loads(text)
    obj.setdefault("privacy", {})["workHosts"] = hosts
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _apply(name: str, text: str, a: Answers) -> str:
    if name == "config.yaml":
        return set_yaml_keys(text, {
            "owner_name": a.owner_name,
            "owner_talk_name": a.owner_talk_name,
            "vault_root": a.vault_root,
            "private_dir": a.private_dir,
        })
    if name == "app-config.json":
        return set_privacy_hosts(text, a.meeting_hosts)
    return text


def init_data_dir(data_dir: Path, answers: Answers) -> list[str]:
    """Создать каталог данных; вернуть имена созданного (существующее пропускается молча)."""
    created: list[str] = []
    data_dir = Path(data_dir)
    if not data_dir.exists():
        data_dir.mkdir(parents=True)
        data_dir.chmod(0o700)            # mode в mkdir режется umask — выставляем явно
    for sub in SUBDIRS:
        d = data_dir / sub
        if not d.exists():
            d.mkdir()
            created.append(sub)
    for name, example in EXAMPLES.items():
        target = data_dir / name
        if target.exists():
            continue
        shutil.copyfile(example, target)
        target.write_text(_apply(name, target.read_text(encoding="utf-8"), answers), encoding="utf-8")
        created.append(name)
    log = data_dir / "quality-log.md"
    if not log.exists():
        log.write_text(QUALITY_LOG_HEADER, encoding="utf-8")
        created.append("quality-log.md")
    return created
