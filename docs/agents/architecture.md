# Карта кода транскрибатора (для агентов)

Локальный транскрибатор встреч: Swift menu-bar приложение пишет микрофон и системный звук в каталог сессий, Python-воркер распознаёт речь (whisper MLX), делит по голосам, подписывает имена из Толка и кладёт транскрипт в вольт Obsidian. Монолит из двух процессов, без сети. Код ≈ 4,0k строк Python + 3,8k Swift; тесты ≈ 4,5k Python + 1,1k Swift.

## Процессы и хранилище

| Часть | Где | Запуск |
|---|---|---|
| Приложение `Transcriber.app` (menu bar) | `app/`, SwiftPM, таргеты `TranscriberCore` (логика, тестируется) и `Transcriber` (AppKit UI) | launchd `<LABEL_PREFIX>.app`, `scripts/build-app.sh` |
| Воркер `transcriber` (демон + CLI) | `worker/transcriber/`, uv | launchd `<LABEL_PREFIX>.worker`, `scripts/install-worker.sh` |
| Каталог данных | `~/Library/Application Support/Transcriber/` (`TRANSCRIBER_DATA` переопределяет): `config.yaml`, `app-config.json`, `glossary.txt`, `.env`, `install.env` (`LABEL_PREFIX`), `sessions/`, `voices/`, `bench/`, `quality-log.md`, `quality-backlog.md` | создаёт `transcriber init` |
| Логи | `~/Library/Logs/transcriber-app.log`, `transcriber-worker.log`, `transcriber-ax/` | — |

Протокол между процессами — файлы сессии `sessions/<id>/`: `meta.json` (пишет приложение), `mic.pcm`/`sys.pcm`, `talk.jsonl` (плитки Толка), `STOP` (просьба финализировать), `state.json` (пишет воркер: реплики, статус, `note_path`) и `embeddings.npz` (эмбеддинги обоих каналов, float32; у сессий до 14.10 они внутри `state.json`).

## worker/transcriber

- `config.py` — `Config` (все пороги, пути, владелец), `DATA_DIR`/`DEFAULT_DATA_DIR`/`CODE_ROOT`, `load_config()` читает `DATA_DIR/config.yaml`, `glossary.txt`, `.env`. Примеры: `worker/config.example.yaml`, `glossary.example.txt`, `app-config.example.json` в корне репо.
- `firstrun.py` — `transcriber init`: `Answers`, `ask()`, `init_data_dir()`, `set_yaml_keys()`, `set_privacy_hosts()`, `QUALITY_LOG_HEADER`.
- `cli.py` — argparse: `run`, `from-wav`, `finalize`, `refinalize`, `retranscribe`, `make-private`, `learn`, `voices`, `forget`, `bench`, `bench-diar`, `bench-ref`, `bench-diff`, `init`.
- `daemon.py` — цикл демона: следит за `sessions/`, `Pipeline.step()`, лимиты памяти (`memory.py`), брошенные записи; `reload_config` перечитывает `config.yaml` при старте новой сессии.
- `pipeline.py` — `Pipeline`: VAD (`vad.py`) → блоки (`blocks.py`) → ASR (`asr.py`, промпт из глоссария) → фильтры (`quality.py`, `echo.py`, `aec.py`) → эмбеддинги/кластеры (`speakers.py`: `recluster`, `absorb_small_clusters`, `split_by_names` — разрез склейки по плиткам; `recluster_embeddings`/`split_merged_clusters` в `pipeline.py` — та же цепочка, что на стенде) → имена (`talk.py`, `voices.py`, `owner.py`) → Raw и заглушка MTG (`vault.py`).
- `session.py` — `Session`, `State`, `Meta`, блокировка сессии; `save_state` пишет `state.json` без массивов + `save_embeddings`/`load_embeddings` (сайдкар `embeddings.npz`); `pcm.py` — чтение PCM; `models.py` — загрузка моделей.
- `bench.py` — стенд качества по `bench_dir`, пишет git sha из `CODE_ROOT`.
- `judge.py` — судья контекста (Claude через SDK): `judge_chunk` (случайный порядок A/B, разбор JSON), `summarize`; ключ только из окружения/.env каталога данных.
- `bench_ref.py` — стенд сравнения с ботом Толка: `overlap_chunks` (общее окно и выровненные отрезки), `judge_session`, `find_reference` (заметка той же встречи), `parse_reference`/`parse_local`, `compare_transcripts` (общие слова, покрытие, согласие говорящих, имена, пропущенные куски), `run_bench_ref` → `bench_dir/ref/runs/NN-<label>/`.
- `bench_diar.py` — стенд диаризации: `evaluate_session` (перекластеризация `state.json` текущим конфигом + имена плиток из `talk.jsonl`), `session_metrics` (чистота, цельность, склейки, расколы), `run_bench_diar` → `bench_dir/diar/runs/NN-<label>/`, таблица `format_table`; `--holdout` — разрез по чётным репликам, метрика по нечётным.
- `vault.py` — `write_raw`, `ensure_stub`, `render_stub`, `THOUGHTS` (заголовок секции владельца; вольт ищет его дословно).

## app/Sources

- `TranscriberCore/AppPaths.swift` — `dataDir`, `appConfig`, `sessions`, `workerConfig` (единственное место, знающее каталог данных).
- `PrivacyRules.swift` — `PrivateWindow` (+ `parse(line:)`/`line`), `PrivacyRules` (`isPrivate`, `load(from:)` пишет умолчания в пустой файл), `TalkConfig.swift`, `ReconConfig.swift` — свои блоки того же `app-config.json`.
- `Settings.swift` — `Settings`, `SettingsStore.load/save`, `settingYAML`, `settingPrivacy` (редактирует файлы, не разрушая комментарии и чужие ключи).
- `SessionStore.swift` — `SessionDir` (`notePath`/`rawPath`/`status` из `state.json`), `SessionStore.create` (meta.json), `defaultRoot`; `LiveTranscript.swift` — текст, распознанный к этому моменту: Raw по `raw_path`, всё после заголовка `## Полный транскрипт` (тот же, что пишет `vault.render_raw`).
- `MeetingDetector.swift` + `AudioProcesses.swift` — кто держит микрофон; `BrowserTabs.swift` — вкладка со звонком (`defaultMeetingHosts` + хосты из конфига), `TalkInspector.swift`/`TalkObserver.swift`/`AXDump.swift`/`AXRecon.swift` — AX-дерево Толка, `talk.jsonl`.
- `TapCapture.swift` (UID агрегатного устройства = bundle id + `.agg`), `AudioDevices.swift`, `Downmixer.swift`, `PcmWriter.swift` — захват и запись звука; `PowerHold.swift` — не спать; `Obsidian.swift` — открыть заметку; `PromptPolicy.swift` — судьба карточки «Записать?»; `Log.swift`.
- `Transcriber/AppDelegate.swift` — оркестрация (детектор, карточки, старт/стоп, AX-наблюдатели); `SessionController.swift` — одна запись; `StatusItem.swift` — меню (запись, приватная, заметка, «Настройки…», папка сессий, выход); `SettingsWindow.swift` — окно настроек; `PromptPanel.swift`, `FloatingButton.swift` (кружок записи: клик — окно текста, правый клик — меню действий), `TranscriptWindow.swift` (окно с текстом записи, перечитывает Raw раз в 2 с, панель «Открыть заметку / Остановить / Скрыть кнопку»); `main.swift` — `--record-test`, `--prompt-test`, `--transcript-test <каталог сессии>` (снимок окна текста).

## Скрипты, установка, спайк

`scripts/install-lib.sh` (общий для обоих: `DATA_DIR`, `LABEL_PREFIX` — переменная `TRANSCRIBER_LABEL_PREFIX` > `install.env` каталога данных > `local.transcriber`, `render_template` подставляет `__HOME__`/`__REPO__`/`__LABEL_PREFIX__`), `scripts/install-worker.sh` (uv sync, plist из `launchd/worker.plist`, подсказка про `transcriber init`, если нет `config.yaml`), `scripts/build-app.sh` (сборка, bundle id из `LABEL_PREFIX` в `app/Resources/Info.plist`, подпись «Transcriber Dev», установка, plist из `launchd/app.plist`; отказывается, если в `$DATA_DIR/sessions` идёт запись; `--force`). `launchd/app.plist`, `launchd/worker.plist` — шаблоны с `__HOME__`/`__REPO__`/`__LABEL_PREFIX__`, в `~/Library/LaunchAgents/` попадают только после подстановки. Внешние артефакты и их соседи — `docs/product/integrations.md` (локально, вне git). `spike/whisperkit/` — замороженный спайк сравнения WhisperKit и MLX (`compare.py`, `examples.py`, `measure.py`, `mlx_blocks.py`, пакет `wkspike`); в продукт не входит, тестами не покрыт.

## Тесты

- Воркер: `cd worker && uv run pytest -q --ignore=tests/test_real_models.py`; `conftest.py` — `FakeAsr`, `FakeEmbedder`, `make_session`, фикстуры `vault`, `private_dir`, автоподмена `DATA_DIR` на tmp. Темы: `test_config`, `test_firstrun`, `test_pipeline`, `test_talk`, `test_voices`, `test_owner`, `test_vault`, `test_cli`, `test_bench`, `test_bench_diar`, `test_bench_ref`, `test_judge`, `test_continuation`, `test_daemon_memory`, `test_echo`/`test_aec`, `test_quality`, `test_asr`, `test_blocks`, `test_vad`, `test_speakers`, `test_session`, `test_pcm`, `test_models` (загрузка моделей), `test_install_scripts` (префикс меток: `install.env`, предупреждение о чужом агенте, экранирование в шаблонах), `test_real_models` (живые модели, в обычный прогон не входит).
- Приложение: `cd app && swift test` (swift-testing, XCTest недоступен). `Fixtures/talk-grid.txt` — AX-дамп сетки Толка с вымышленными именами.

## Куда идти по задаче

| Задача | Файлы |
|---|---|
| Новый порог/настройка воркера | `config.py` + `config.example.yaml` + `test_config.py` |
| Где лежат данные | `config.py` (`DATA_DIR`), `AppPaths.swift`, `scripts/*.sh`, README |
| Первичная настройка / меню настроек | `firstrun.py`, `Settings.swift`, `SettingsWindow.swift`, `StatusItem.swift` |
| Приватность | `PrivacyRules.swift`, `SessionController.swift` |
| Детект встречи в браузере | `BrowserTabs.swift`, `AppDelegate.swift` |
| Имена говорящих | `talk.py`, `voices.py`, `owner.py`, `speakers.py` |
| Сравнение с ботом Толка | `bench_ref.py`, `config.yaml: reference_subdir` |
| Качество диаризации (пороги кластеризации) | `speakers.py`, `bench_diar.py` (стенд), `config.example.yaml` |
| Формат Raw/MTG | `vault.py` (+ инструкции вольта, см. integrations.md; заголовок транскрипта читает и `LiveTranscript.swift`) |
| Кружок записи и окно текста | `FloatingButton.swift`, `TranscriptWindow.swift`, `LiveTranscript.swift` |
| Перезапуск встречи, несколько частей | `continuation.py`, `vault.render_meeting`, `daemon.py` |

## Правила для ревью

Никаких реальных имён, хостов компании, расписаний в репо — только вымышленные из плана. Секреты только в `.env` каталога данных. Тесты не читают рабочий каталог данных. Код по-английски, комментарии по-русски.
