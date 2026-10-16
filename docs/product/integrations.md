# Внешние артефакты транскрибатора и их соседи

Кто производит и кто потребляет всё, что живёт вне этого репо. Проверяется при закрытии тикета (building-products, шаг «соседи»).

| Артефакт | Пишет | Читает | Инструкция соседа | Проверено |
|---|---|---|---|---|
| `Memory/50 · Raw/Local/Local-*.md` в вольте — транскрипт с шапкой; с 14.10 строка «На микрофоне кроме владельца: …» | воркер (`vault.write_raw`) | агент `meeting-distiller`, skill `meeting-processing` (вольт `.claude/`) | обе инструкции дополнены 14.10 (коммит `self:` в вольте) | 14.10 |
| `Memory/30 · Meetings/MTG-… .md` заглушка с секцией `owner_thoughts_heading` (по умолчанию «## Мысли владельца», у владельца задано своё в `config.yaml`) | воркер (`vault.ensure_stub`) | `meeting-distiller` (сохраняет секцию дословно), `vault-conventions` | заголовок ищется дословно — при смене `owner_thoughts_heading` надо править обе инструкции вольта | 14.10 |
| `quality-log.md` в каталоге данных (`~/Library/Application Support/Transcriber/`) | рутина `/daily` вольта, шаг 9а | ручная сессия разбора | `.claude/commands/daily.md` шаг 9а, чек-лист | 14.10 |
| `~/Library/Application Support/Transcriber/voices/*.json` — библиотека голосов, с 14.10 профиль владельца с источником `<сессия>:mic` | воркер (`talk_autolearn`, `owner.learn_owner`), `transcriber learn` | воркер (`name_clusters`, `relabel_mic`); стенд `bench` — только пустая копия в песочнице | — | 14.10 |
| `~/Library/Logs/transcriber-app.log` — с 14.10 причина остановки и судьба карточки «Записать?» | приложение | ручная сессия разбора | `quality-log.md` шапка | 14.10 |
| `~/Documents/Приватные записи/<id>/` | воркер | только Владислав; агент содержимое не читает | memory `transcriber-project` | 21.09 |
| `app-config.json` + `config.yaml` в каталоге данных | `transcriber init`, меню «Настройки…» приложения | воркер (`config.yaml`), приложение (`app-config.json`, `config.yaml` для настроек) | `README.md` «Где лежат данные» | 14.10 |
| launchd-агенты `~/Library/LaunchAgents/<LABEL_PREFIX>.{app,worker}.plist` (префикс установки — `install.env` каталога данных) | `scripts/install-worker.sh`, `scripts/build-app.sh` из шаблонов `launchd/*.plist` | launchd | `README.md` «Установка» | 14.10 |
| `Memory/50 · Raw/TalkBot/бот Толка-<дата>-<время>-<название>.md` в вольте — транскрипты бота Толка с ФИО и таймингами | скрипт вольта `.claude/scripts/talkbot_notes.py` (`sync`/`fetch`) | стенд `transcriber bench-ref` (только читает) | README папки `_README.md` называет её эталоном стенда; формат строк `[mm:ss] ФИО: текст` — при смене формата править `bench_ref.parse_reference` | 14.10 |
