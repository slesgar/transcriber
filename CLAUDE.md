# Транскрибатор

Продукт ведёт оркестратор `building-products`.

**В начале каждой сессии:** загрузить skill `building-products` и прочитать `docs/product/STATE.md` — там текущая фаза и следующий шаг. Перед любой работой с кодом — `docs/agents/architecture.md`.

- Рабочие документы мастерской — локально, в git не идут (`.gitignore`): STATE и соседи вне репо (`docs/product/integrations.md`), спеки (`docs/specs/`, общая — `2026-09-15-transcriber-design.md`), планы (`docs/plans/`, `docs/superpowers/`)
- Профиль установки для `leak-check.py` — `instance/profile-sources.txt` (пути к `config.yaml` и `app-config.json` каталога данных; вне git): проверка ищет его значения в коде; умолчание не должно совпадать со значением установки
- Данные установки (конфиги, сессии, голоса, стенд, лог качества) — вне репо, в `~/Library/Application Support/Transcriber/`; в репо только нейтральные примеры. Реальные имена, хосты и расписания в репо не попадают.
- Секреты только в `.env` каталога данных, их кладёт владелец установки.
- Версия — `CFBundleShortVersionString` (`app/Resources/Info.plist`) и `version` (`worker/pyproject.toml`), меняются вместе; `CFBundleVersion` — порядковый номер сборки. Релиз — по разделу «Release» skill `building-products`: `## Unreleased` → `## X.Y.Z — дата` в `CHANGELOG.md`, README, тег `vX.Y.Z`, GitHub release (репо публичный), релиз-ноут для чатов.
