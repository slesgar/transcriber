# Разделение программы и данных установки — план реализации (тикет 12)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** репо содержит только программу; всё, что описывает установку владельца (конфиги, глоссарий, сессии, голоса, стенд, лог качества), живёт в каталоге данных вне репо, с нейтральными умолчаниями и первичной настройкой.

**Architecture:** воркер и приложение берут единый каталог данных `~/Library/Application Support/Transcriber/` (переменная `TRANSCRIBER_DATA` переопределяет). Воркер: `config.DATA_DIR`, `transcriber init` создаёт каталог из примеров `*.example.*`. Приложение: `AppPaths.dataDir`, нейтральные `PrivacyRules.defaults`, пункт меню «Настройки…» пишет `app-config.json` и `config.yaml`. Скрипты установки подставляют `$HOME` и путь к репо в шаблоны plist. Миграция живой установки и переписывание истории git — руками контроллера после мержа, не задачами плана.

**Tech Stack:** Python 3.12 / uv / pytest; Swift 5.9 SwiftPM / swift-testing; bash; launchd.

**Spec:** `docs/specs/2026-10-14-data-dir-separation.md` (раздел «Первичная настройка и настройки» — обязательный).

## Global Constraints

- Каталог данных по умолчанию `~/Library/Application Support/Transcriber/`; переменная окружения `TRANSCRIBER_DATA` переопределяет его и в воркере, и в приложении; тесты всегда указывают её (или подменяют константу) на временный каталог и никогда не читают рабочий каталог.
- Нейтральные умолчания в коде и примерах: `owner_name: Owner`, `owner_talk_name: ""`, `owner_thoughts_heading: "## Мысли владельца"`, `talk_shared_names: []`, `vault_root: ~/Documents/Obsidian Vault`, `private_dir: ~/Documents/Приватные записи`; приложение: `privacy.windows: []`, `privacy.apps: []`, `privacy.workApps: ["kontur.talk"]`, `privacy.workHosts: []`.
- В репо нет ни одного имени коллег, хоста компании, расписания приватных встреч, названий встреч — ни в коде, ни в тестах, ни в фикстурах, ни в документации. Вымышленные значения для тестов и примеров: имена «Анна Петрова», «Иван Сидоров», «Мария Иванова», «Пётр Кузьмин», «Ольга Смирнова», «John Smith»; хост `talk.example.com`; окно приватности `weekday 3, 12:00–12:50`; название встречи «Еженедельный синк»; переговорка «Переговорная 5».
- Вид личных встреч владельца в репо не называется — только «приватная встреча».
- Код и идентификаторы по-английски, комментарии и документация для владельца по-русски (как в репо сейчас); в комментариях кода и тестов владелец зовётся «владелец»/«the owner», не по имени.
- Коммиты по-русски, последняя строка `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Проверка воркера: `cd worker && uv run pytest -q --ignore=tests/test_real_models.py` (сейчас 317 passed); приложения: `cd app && swift test` (сейчас 98). Тяжёлые модели и стенд не запускать.
- Каждая задача, добавляющая или переименовывающая файл, правит `docs/agents/architecture.md` в том же коммите.

## Review Focus

1. `TRANSCRIBER_DATA` задан, но каталога нет: воркер и приложение должны создать то, что пишут (`sessions/`, `app-config.json`), а не падать — тест в Task 1 (`test_load_config_with_missing_data_dir_uses_defaults`) и Task 3 (`PrivacyRules.load` пишет умолчания в несуществующий каталог).
2. Повторный `transcriber init` поверх живого каталога не трогает ни одного существующего файла, даже если ответы другие — Task 2 `test_init_is_idempotent`.
3. Старый `app-config.json` без `workHosts`: пустое умолчание означает, что звонок в Толке в браузере перестаёт считаться рабочим, пока хост не вписан; `init`/«Настройки…» обязаны спросить хосты, а README — объяснить последствие — Task 2, Task 4, Task 5.
4. «Настройки…» пишут `config.yaml`, не разрушая комментарии и неизвестные ключи (пороги остаются на месте) — Task 4 `settingYAML keeps comments and unknown keys`.
5. Скрипты установки запускаются из любого чекаута: plist получает путь того репо, из которого запущен скрипт, а не захардкоженный — Task 5 проверка `grep -c "__HOME__\|__REPO__" ~/Library/LaunchAgents/…` = 0.

---

### Task 1: Каталог данных в воркере, нейтральные умолчания, примеры конфигов

**Files:**
- Modify: `worker/transcriber/config.py`, `worker/transcriber/bench.py:29,248-250`, `worker/transcriber/asr.py:23`, `worker/transcriber/quality.py:33`, `worker/transcriber/vault.py:17,76,112,122`
- Rename: `worker/config.yaml` → `worker/config.example.yaml`; `glossary.txt` → `glossary.example.txt`
- Create: `app-config.example.json`
- Modify: `worker/tests/conftest.py:131-135`, `worker/tests/test_config.py`, `.gitignore`, `docs/agents/architecture.md`
- Test: `worker/tests/test_config.py`

**Interfaces:**
- Produces: `config.CODE_ROOT: Path` (корень репо, `Path(__file__).resolve().parents[2]`), `config.DEFAULT_DATA_DIR = Path("~/Library/Application Support/Transcriber")`, `config.DATA_DIR: Path` (из `TRANSCRIBER_DATA` или `DEFAULT_DATA_DIR`, `expanduser()`), `Config.sessions_dir/voices_dir/bench_dir` — `default_factory` от `DATA_DIR`; `load_config()` читает `DATA_DIR/config.yaml`, `DATA_DIR/glossary.txt`, `DATA_DIR/.env`. Файлы-примеры: `CODE_ROOT/worker/config.example.yaml`, `CODE_ROOT/glossary.example.txt`, `CODE_ROOT/app-config.example.json`.

- [ ] **Step 1: Переписать тесты конфига** в `worker/tests/test_config.py`: удалить `test_root_can_be_overridden_by_env` и `test_owner_defaults_reproduce_vladislavs_install`; добавить

```python
def test_data_dir_can_be_overridden_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIBER_DATA", str(tmp_path))
    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.DATA_DIR == tmp_path
        cfg = reloaded.Config()
        assert (cfg.sessions_dir, cfg.voices_dir, cfg.bench_dir) == (tmp_path / "sessions", tmp_path / "voices", tmp_path / "bench")
    finally:
        monkeypatch.delenv("TRANSCRIBER_DATA")
        importlib.reload(config_module)

def test_default_data_dir_is_application_support():
    assert config_module.DEFAULT_DATA_DIR == Path("~/Library/Application Support/Transcriber")

def test_load_config_reads_from_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path)
    (tmp_path / "config.yaml").write_text("min_silence_ms: 321\n")
    (tmp_path / "glossary.txt").write_text("Толк\n")
    cfg = load_config()
    assert cfg.min_silence_ms == 321 and cfg.glossary == ["Толк"]

def test_load_config_with_missing_data_dir_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "nope")
    cfg = load_config()
    assert cfg.min_silence_ms == 700 and cfg.sessions_dir == tmp_path / "nope" / "sessions"

def test_owner_defaults_are_neutral():
    cfg = Config()
    assert cfg.owner_name == "Owner" and cfg.owner_thoughts_heading == "## Мысли владельца"
    assert cfg.owner_talk_name == "" and cfg.talk_shared_names == []
    assert cfg.vault_root == Path("~/Documents/Obsidian Vault").expanduser()

def test_example_yaml_is_neutral():
    data = _yaml.safe_load((config_module.CODE_ROOT / "worker" / "config.example.yaml").read_text())
    assert data["owner_name"] == "Owner" and data["talk_shared_names"] == []
    assert data.get("owner_talk_name", "") == "" and data["owner_thoughts_heading"] == "## Мысли владельца"
```

Все `from transcriber.config import ROOT` / `ROOT / "worker" / "config.yaml"` в этом файле → `config_module.CODE_ROOT / "worker" / "config.example.yaml"`; `assert cfg.vault_root.name == "Вольт воркфлоу"` → `"Obsidian Vault"`; в `test_owner_talk_name_joins_shared_names` имена → «Анна Петрова», «Переговорная 5».

- [ ] **Step 2: Запустить `cd worker && uv run pytest -q tests/test_config.py`** — ожидание: FAIL (нет `DATA_DIR`, `CODE_ROOT`, примера).

- [ ] **Step 3: `config.py`**: `ROOT`/`VOICES_DIR`/`TRANSCRIBER_ROOT` убрать; добавить `CODE_ROOT`, `DEFAULT_DATA_DIR`, `DATA_DIR` (см. Interfaces); `sessions_dir`, `bench_dir`, `voices_dir` — `field(default_factory=lambda: DATA_DIR / "…")` (лямбда читает глобал в момент вызова — так тесты подменяют каталог через `monkeypatch.setattr`); `vault_root` по умолчанию `~/Documents/Obsidian Vault`; `owner_name = "Owner"`, `owner_thoughts_heading = "## Мысли владельца"`; `load_config`: `load_dotenv(DATA_DIR / ".env")`, `yaml_path or DATA_DIR / "config.yaml"`, `glossary_path or DATA_DIR / "glossary.txt"`. Комментарий к `DATA_DIR`: что лежит в каталоге и что `TRANSCRIBER_DATA` нужен тестам и второй установке.

- [ ] **Step 4: Остальные умолчания владельца**: `asr.py:23`, `quality.py:33`, `vault.py:76,112,122` — `owner: str = "Owner"`; `vault.THOUGHTS = "## Мысли владельца"`; `bench.py` — `from .config import Config, CODE_ROOT` и `git -C CODE_ROOT`; докстринг `quality.py:5-6` — пример галлюцинации с вымышленными словами («Owner, Ромашка, Омни, CDP, Факт, Анна Петрова.»).

- [ ] **Step 5: Примеры**: `git mv worker/config.yaml worker/config.example.yaml` и отредактировать: `vault_root: ~/Documents/Obsidian Vault`, `talk_shared_names: []`, `owner_name: Owner`, `owner_talk_name: ""`, `owner_thoughts_heading: "## Мысли владельца"`, `talk_room_patterns: ["^\\d{3}", "переговор", "room"]`; комментарий про библиотеку голосов — «в `voices/` каталога данных»; шапка файла: «пример; рабочая копия — `config.yaml` в каталоге данных, создаёт `transcriber init`». `git mv glossary.txt glossary.example.txt` с содержимым `Толк\nObsidian\nZoom\n`. Создать `app-config.example.json` — тот же объект, что пишет `PrivacyRules.writeDefaults` + блоки `recon` и `talk` (тексты `_comment` взять из `PrivacyRules.swift:140-144`, `ReconConfig.swift:84-86`, `TalkConfig.swift:67-69`), значения: `privacy.windows: []`, `privacy.apps: []`, `privacy.workApps: ["kontur.talk"]`, `privacy.workHosts: []`, `recon: {enabled: true, seconds: 180}`, `talk: {enabled: true}`; ключи отсортированы, отступ 2 пробела (как пишет приложение).

- [ ] **Step 6: Тесты и .gitignore**: `conftest.py` — фикстура `isolated_voices` → `isolated_data_dir`: `monkeypatch.setattr(config_module, "DATA_DIR", tmp_path)` и `monkeypatch.setenv("TRANSCRIBER_DATA", str(tmp_path))`, докстринг без упоминания рабочего пути. `.gitignore`: убрать `app-config.json`, `sessions/`, `voices/`, `bench/`, `worker.log` (данные больше не лежат в репо), оставить `.env`.

- [ ] **Step 7: Полный прогон** `cd worker && uv run pytest -q --ignore=tests/test_real_models.py`. Тесты, которые проверяли старые умолчания владельца («Владислав», «## Мысли Владислава») — ожидаемо `test_vault.py`, `test_quality.py`, возможно `test_pipeline.py`, `test_owner.py`, `test_cli.py` — перевести на нейтральные (`Owner`, `## Мысли владельца`) **или** на явный `Config(owner_name="Анна")`, если тест проверяет подстановку имени. Ожидание: все зелёные, число тестов ≥ 317 + 4.

- [ ] **Step 8: Карта** `docs/agents/architecture.md`: строка про `config.py` (DATA_DIR, CODE_ROOT, примеры). **Коммит** `worker: каталог данных ~/Library/Application Support/Transcriber, нейтральные умолчания и примеры конфигов`.

---

### Task 2: `transcriber init` — первичная настройка

**Files:**
- Create: `worker/transcriber/firstrun.py`, `worker/tests/test_firstrun.py`
- Modify: `worker/transcriber/cli.py:372-410` (подкоманда), докстринг модуля `cli.py:1-40` (строка про `init`), `docs/agents/architecture.md`

**Interfaces:**
- Consumes: `config.DATA_DIR`, `config.CODE_ROOT` (Task 1).
- Produces (`firstrun.py`):
  - `@dataclass class Answers: owner_name: str = "Owner"; owner_talk_name: str = ""; vault_root: str = "~/Documents/Obsidian Vault"; private_dir: str = "~/Documents/Приватные записи"; meeting_hosts: list[str] = field(default_factory=list)`
  - `EXAMPLES: dict[str, Path]` — `{"config.yaml": CODE_ROOT/"worker"/"config.example.yaml", "app-config.json": CODE_ROOT/"app-config.example.json", "glossary.txt": CODE_ROOT/"glossary.example.txt"}`; `SUBDIRS = ("sessions", "voices", "bench")`
  - `ask(defaults: Answers, read: Callable[[str], str] = input, write: Callable[[str], None] = print) -> Answers` — пять вопросов по-русски с умолчанием в скобках; пустой ввод оставляет умолчание; хосты через запятую.
  - `init_data_dir(data_dir: Path, answers: Answers) -> list[str]` — создаёт каталог (0700) и `SUBDIRS`, копирует каждый пример, которого ещё нет, применяет ответы **только к созданным в этом вызове** файлам, создаёт `quality-log.md` с шапкой, если его нет; возвращает имена созданных файлов/папок (существующие пропускаются молча).
  - `set_yaml_keys(text: str, values: dict[str, object]) -> str` — строка `^key:` заменяется на `key: <yaml-скаляр>` (строки в двойных кавычках через `json.dumps(ensure_ascii=False)`, списки в flow-стиле `["a", "b"]`, `bool` как `true/false`), комментарии и остальные строки на месте; отсутствующий ключ дописывается в конец.
  - `set_privacy_hosts(text: str, hosts: list[str]) -> str` — `json.loads`, `obj["privacy"]["workHosts"] = hosts`, `json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"`.
  - `QUALITY_LOG_HEADER: str` — по-русски: назначение файла, формат раздела `## DD.MM.YYYY (прогон DD.MM)` и строки `- HH:MM — <симптом>`; правило «дата, время, симптом — без имён участников и тем встреч».
  - Ответы ложатся так: `config.yaml` ← `owner_name`, `owner_talk_name`, `owner_thoughts_heading = f"## Мысли {owner_name}"`, `vault_root`, `private_dir`; `app-config.json` ← `privacy.workHosts = meeting_hosts`.
- CLI: `transcriber init [--yes] [--owner NAME] [--owner-talk-name NAME] [--vault PATH] [--private-dir PATH] [--hosts a,b]`; без `--yes` — `ask()` с флагами как умолчаниями; с `--yes` — без вопросов. Печатает каталог и список созданного; при пустом списке — «уже настроено: <каталог>».

- [ ] **Step 1: Тесты** `worker/tests/test_firstrun.py`:

```python
def test_init_creates_files_from_examples_and_applies_answers(tmp_path):
    created = init_data_dir(tmp_path / "data", Answers(owner_name="Анна", owner_talk_name="Анна Петрова",
                            vault_root="~/V", private_dir="~/P", meeting_hosts=["talk.example.com"]))
    assert set(created) >= {"config.yaml", "app-config.json", "glossary.txt", "quality-log.md", "sessions", "voices", "bench"}
    data = yaml.safe_load((tmp_path / "data" / "config.yaml").read_text())
    assert data["owner_name"] == "Анна" and data["owner_talk_name"] == "Анна Петрова"
    assert data["owner_thoughts_heading"] == "## Мысли Анна" and data["vault_root"] == "~/V" and data["private_dir"] == "~/P"
    assert data["speaker_similarity"] == 0.3            # остальное из примера
    app = json.loads((tmp_path / "data" / "app-config.json").read_text())
    assert app["privacy"]["workHosts"] == ["talk.example.com"] and app["recon"]["enabled"] is True
    assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700

def test_init_is_idempotent(tmp_path):
    init_data_dir(tmp_path, Answers(owner_name="Анна"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert init_data_dir(tmp_path, Answers(owner_name="Пётр", meeting_hosts=["x.example.com"])) == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before

def test_set_yaml_keys_keeps_comments_and_replaces_in_place():
    text = "# шапка\nowner_name: Owner   # кто\nspeaker_similarity: 0.3\n"
    out = set_yaml_keys(text, {"owner_name": "Анна", "talk_shared_names": ["Переговорная 5"]})
    assert out.splitlines()[0] == "# шапка" and 'owner_name: "Анна"' in out and "speaker_similarity: 0.3" in out
    assert out.rstrip().endswith('talk_shared_names: ["Переговорная 5"]')
    assert yaml.safe_load(out)["owner_name"] == "Анна"

def test_ask_keeps_defaults_on_empty_input():
    answers = iter(["", "Анна Петрова", "", "", "talk.example.com, ktalk.ru"])
    got = ask(Answers(), read=lambda prompt: next(answers), write=lambda s: None)
    assert got.owner_name == "Owner" and got.owner_talk_name == "Анна Петрова"
    assert got.meeting_hosts == ["talk.example.com", "ktalk.ru"]

def test_cli_init_yes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "d")
    main(["init", "--yes", "--owner", "Анна", "--hosts", "talk.example.com"])
    assert yaml.safe_load((tmp_path / "d" / "config.yaml").read_text())["owner_name"] == "Анна"
    assert "config.yaml" in capsys.readouterr().out

def test_quality_log_header_forbids_names_and_topics():
    assert "без имён" in QUALITY_LOG_HEADER and "- HH:MM — " in QUALITY_LOG_HEADER
```

- [ ] **Step 2: Прогнать** `uv run pytest -q tests/test_firstrun.py` — FAIL (нет модуля).
- [ ] **Step 3: Реализовать `firstrun.py`** по Interfaces; `init_data_dir` читает `DATA_DIR` только через аргумент — вызывающий `cli.main` передаёт `config_module.DATA_DIR` (глобал модуля, не импортированное имя — иначе monkeypatch в тесте не сработает).
- [ ] **Step 4: CLI**: подпарсер `init` с флагами; ветка в `main` **до** остальных команд; `load_config()` при отсутствующем каталоге возвращает умолчания — не падает. Докстринг модуля: строка `* transcriber init — создать каталог данных из примеров и спросить минимум (владелец, его имя в Толке, вольт, приватные записи, хосты встреч); повторный запуск существующие файлы не трогает`.
- [ ] **Step 5: Прогон** `uv run pytest -q tests/test_firstrun.py tests/test_cli.py tests/test_config.py` — PASS; затем полный набор воркера — PASS.
- [ ] **Step 6: Карта** (`firstrun.py`, `test_firstrun.py`), **коммит** `worker: transcriber init — каталог данных из примеров и первичная настройка`.

---

### Task 3: Приложение — каталог данных и нейтральные правила

**Files:**
- Create: `app/Sources/TranscriberCore/AppPaths.swift`, `app/Tests/TranscriberCoreTests/AppPathsTests.swift`
- Modify: `app/Sources/TranscriberCore/PrivacyRules.swift:1-52,108-112`, `app/Sources/TranscriberCore/SessionStore.swift:34-36`, `app/Sources/TranscriberCore/BrowserTabs.swift:17-45` и его AppleScript-обёртки (`meetingTabState(forBrowser:)`, `meetingInfo(forBrowser:)`, `hasWorkMeeting(forBrowser:hosts:)`), `app/Sources/Transcriber/AppDelegate.swift:238,392,507-511`, `app/Sources/Transcriber/SessionController.swift:95-99`; комментарии в `TalkConfig.swift:5`, `ReconConfig.swift:5,57`, `PromptPolicy.swift:16`
- Test: `PrivacyRulesTests.swift`, `BrowserTabsTests.swift`, `AXReconTests.swift:184,207`, `TalkObserverTests.swift:20`

**Interfaces:**
- Produces: `public enum AppPaths { static let dataDirEnvironment = "TRANSCRIBER_DATA"; static var dataDir: URL; static var appConfig: URL /* dataDir/app-config.json */; static var sessions: URL /* dataDir/sessions */; static var workerConfig: URL /* dataDir/config.yaml */ }` — `dataDir` читает `ProcessInfo.processInfo.environment["TRANSCRIBER_DATA"]` (непустое → `URL(fileURLWithPath:)`), иначе `~/Library/Application Support/Transcriber`.
- `PrivacyRules.defaultWorkApps = ["kontur.talk"]`, `defaultWorkHosts: [String] = []`, `defaults = PrivacyRules(windows: [], apps: [])`, `defaultURL = AppPaths.appConfig`. `SessionStore.defaultRoot = AppPaths.sessions`.
- `BrowserTabs.defaultMeetingHosts = ["ktalk.ru", "talk.kontur.ru", "meet.google.com", "telemost.yandex.ru", "zoom.us"]`; `isMeeting(_ tab: BrowserTab, hosts: [String] = defaultMeetingHosts) -> Bool`; `pickMeeting(_ tabs: [BrowserTab], hosts: [String] = defaultMeetingHosts) -> BrowserTab?`; `isWorkMeeting(_ tab: BrowserTab, hosts: [String]) -> Bool` — вкладка на одном из `hosts` с комнатой в пути (`isMeeting(tab, hosts: defaultMeetingHosts + hosts)` и хост ∈ `hosts`); обёртки для браузера получают параметр `hosts:`; вызывающие (`AppDelegate`, `SessionController`) передают `BrowserTabs.defaultMeetingHosts + PrivacyRules.load().workHosts`, чтобы корпоративный хост из конфига считался встречей.

- [ ] **Step 1: Тесты**. `AppPathsTests.swift`:

```swift
@Test func dataDirFollowsEnvironment() {
    setenv("TRANSCRIBER_DATA", "/tmp/transcriber-test-data", 1); defer { unsetenv("TRANSCRIBER_DATA") }
    #expect(AppPaths.dataDir.path == "/tmp/transcriber-test-data")
    #expect(AppPaths.appConfig.lastPathComponent == "app-config.json")
    #expect(AppPaths.sessions.path == "/tmp/transcriber-test-data/sessions")
}
@Test func dataDirDefaultsToApplicationSupport() {
    unsetenv("TRANSCRIBER_DATA")
    #expect(AppPaths.dataDir.path.hasSuffix("/Library/Application Support/Transcriber"))
}
```

`PrivacyRulesTests.swift`: `@Test func defaultsAreNeutral()` — `PrivacyRules.defaults.windows.isEmpty`, `.apps.isEmpty`, `.workHosts.isEmpty`, `.workApps == ["kontur.talk"]`, и `defaults.isPrivate(app: "us.zoom", at: Date()) == false`; существующие тесты со старым окном → `weekday 3 12:00–12:50`, хосты → `talk.example.com`; `@Test func loadWritesDefaultsIntoMissingDirectory()` — `url = tmp/nested/app-config.json`, `PrivacyRules.load(from: url)` создаёт файл. `BrowserTabsTests.swift`: все корпоративные хосты → `talk.example.com`, передаваемый через `hosts: BrowserTabs.defaultMeetingHosts + ["talk.example.com"]`; новый `@Test func configuredHostCountsAsMeetingOnlyWhenPassed()` — `isMeeting(tab("https://talk.example.com/room1")) == false` и `== true` с `hosts:`; названия встреч → «Еженедельный синк — Толк». `AXReconTests.swift:184,207` — окно `weekday 3 12:00–12:50`.

- [ ] **Step 2: `cd app && swift build --build-tests`** — ошибки компиляции (нет `AppPaths`, сигнатур).
- [ ] **Step 3: Реализовать** `AppPaths.swift`, правки `PrivacyRules`, `SessionStore`, `BrowserTabs` и вызывающих по Interfaces. Комментарии: «a private appointment» → «a private call», «Владислав» → «the owner»; описание `PrivateWindow` — «a recurring private slot in the week (a regular personal appointment)». Текст `_comment` в `writeDefaults` оставить (он уже нейтральный).
- [ ] **Step 4: `cd app && swift test`** — PASS, ≥ 98 + 4.
- [ ] **Step 5: Карта** (`AppPaths.swift`), **коммит** `app: каталог данных через AppPaths, нейтральные правила приватности, хосты встреч из конфига`.

---

### Task 4: Меню «Настройки…»

**Files:**
- Create: `app/Sources/TranscriberCore/Settings.swift`, `app/Sources/Transcriber/SettingsWindow.swift`, `app/Tests/TranscriberCoreTests/SettingsTests.swift`
- Modify: `app/Sources/Transcriber/StatusItem.swift:5-32` (пункт меню, колбэк `onOpenSettings`), `app/Sources/Transcriber/AppDelegate.swift` (подключение колбэка), `app/Sources/TranscriberCore/PrivacyRules.swift` (`PrivateWindow` — `parse(line:)`/`line`), `docs/agents/architecture.md`

**Interfaces:**
- Consumes: `AppPaths.appConfig`, `AppPaths.workerConfig`, `PrivacyRules`, `PrivateWindow` (Task 3).
- Produces (`Settings.swift`):
  - `public struct Settings: Equatable { var ownerName, ownerTalkName, vaultRoot, privateDir: String; var workHosts: [String]; var privateWindows: [PrivateWindow]; var privateApps: [String] }`
  - `public enum SettingsStore { static func load(appConfig: URL = AppPaths.appConfig, workerConfig: URL = AppPaths.workerConfig) -> Settings; static func save(_ s: Settings, appConfig: URL = AppPaths.appConfig, workerConfig: URL = AppPaths.workerConfig) throws }` — `load`: приватность через `PrivacyRules.load(from:)`, YAML-ключи `owner_name`, `owner_talk_name`, `vault_root`, `private_dir` строчным парсером (`^key:\s*(.*)$`, снятые кавычки); отсутствующий файл → нейтральные умолчания. `save`: `settingYAML` для четырёх ключей плюс `owner_thoughts_heading = "## Мысли <ownerName>"`; `settingPrivacy` для `privacy.windows/apps/workHosts`; запись атомарная; отсутствующий `config.yaml` создаётся из ключей.
  - `static func settingYAML(_ text: String, values: [(key: String, value: String)]) -> String` — тот же контракт, что `firstrun.set_yaml_keys` (строка `^key:` заменяется, комментарии и чужие ключи на месте, отсутствующие дописываются; строки в двойных кавычках).
  - `static func settingPrivacy(_ text: String, windows: [PrivateWindow], apps: [String], workHosts: [String]) -> String?` — `JSONSerialization` объект, заменить три поля внутри `privacy` (создать блок, если нет), остальные члены (`_comment`, `recon`, `talk`, `workApps`) не трогать, сериализация `[.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]`; `nil`, если текст не JSON-объект.
  - `PrivateWindow.parse(line: String) -> PrivateWindow?` для формата `"<weekday 1–7> HH:mm-HH:mm"` (например `3 12:00-12:50`), `var line: String` — обратно.
- UI (`SettingsWindow.swift`): `final class SettingsWindow: NSWindowController` с `static let shared`; `func show()` — загружает `SettingsStore.load()`, окно с `NSGridView`: «Имя владельца», «Имя в Толке», «Вольт (путь)», «Приватные записи (путь)», «Хосты встреч (через запятую)», «Приватные приложения (bundle id через запятую)», «Окна приватности (по строке: день HH:mm-HH:mm, 1=вс … 7=сб)» — `NSTextView`; кнопки «Отмена», «Сохранить» (default). «Сохранить» → `SettingsStore.save`; ошибка → `NSAlert`; успех → `Log.shared.info("settings saved")`, окно закрывается. Активировать приложение (`NSApp.activate`) перед показом — menu-bar app без Dock.
- `StatusItem`: пункт `Настройки…` (keyEquivalent `,`) после «Открыть заметку», перед «Папка сессий»; `var onOpenSettings: (() -> Void)?`. `AppDelegate`: `status.onOpenSettings = { SettingsWindow.shared.show() }` рядом с другими колбэками.

- [ ] **Step 1: Тесты** `SettingsTests.swift`:

```swift
@Test func yamlEditorKeepsCommentsAndUnknownKeys() {
    let text = "# шапка\nowner_name: Owner   # кто\nspeaker_similarity: 0.3\n"
    let out = SettingsStore.settingYAML(text, values: [("owner_name", "Анна"), ("vault_root", "~/V")])
    #expect(out.hasPrefix("# шапка\n")); #expect(out.contains("owner_name: \"Анна\"")); #expect(out.contains("speaker_similarity: 0.3"))
    #expect(out.hasSuffix("vault_root: \"~/V\"\n"))
}
@Test func privacyEditorReplacesOnlyPrivacyFields() {
    let text = #"{"_comment": "c", "privacy": {"apps": ["us.zoom"], "windows": [], "workApps": ["kontur.talk"], "workHosts": []}, "recon": {"enabled": true, "seconds": 180}}"#
    let out = SettingsStore.settingPrivacy(text, windows: [PrivateWindow(weekday: 3, from: "12:00", to: "12:50")], apps: [], workHosts: ["talk.example.com"])!
    let obj = try! JSONSerialization.jsonObject(with: out.data(using: .utf8)!) as! [String: Any]
    #expect((obj["recon"] as! [String: Any])["seconds"] as! Int == 180); #expect(obj["_comment"] as! String == "c")
    let privacy = obj["privacy"] as! [String: Any]
    #expect(privacy["workHosts"] as! [String] == ["talk.example.com"]); #expect(privacy["workApps"] as! [String] == ["kontur.talk"])
    #expect((privacy["windows"] as! [[String: Any]]).count == 1)
}
@Test func saveThenLoadRoundTrips() throws {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let s = Settings(ownerName: "Анна", ownerTalkName: "Анна Петрова", vaultRoot: "~/V", privateDir: "~/P",
                     workHosts: ["talk.example.com"], privateWindows: [PrivateWindow(weekday: 3, from: "12:00", to: "12:50")], privateApps: ["us.zoom"])
    try SettingsStore.save(s, appConfig: dir.appendingPathComponent("app-config.json"), workerConfig: dir.appendingPathComponent("config.yaml"))
    #expect(SettingsStore.load(appConfig: dir.appendingPathComponent("app-config.json"), workerConfig: dir.appendingPathComponent("config.yaml")) == s)
    #expect(try String(contentsOf: dir.appendingPathComponent("config.yaml")).contains("owner_thoughts_heading: \"## Мысли Анна\""))
}
@Test func privateWindowLineRoundTrip() {
    #expect(PrivateWindow.parse(line: "3 12:00-12:50") == PrivateWindow(weekday: 3, from: "12:00", to: "12:50"))
    #expect(PrivateWindow(weekday: 3, from: "12:00", to: "12:50").line == "3 12:00-12:50")
    #expect(PrivateWindow.parse(line: "8 12:00-12:50") == nil); #expect(PrivateWindow.parse(line: "пн") == nil)
}
```

- [ ] **Step 2: `swift build --build-tests`** — ошибки компиляции.
- [ ] **Step 3: Реализовать** `Settings.swift`, `PrivateWindow.parse/line`, `SettingsWindow.swift`, пункт меню и колбэк.
- [ ] **Step 4: `cd app && swift test`** — PASS; `swift build -c release --product Transcriber` — собирается.
- [ ] **Step 5: Карта** (`Settings.swift`, `SettingsWindow.swift`, `SettingsTests.swift`), **коммит** `app: меню «Настройки…» — владелец, пути, хосты и окна приватности в app-config.json и config.yaml`.

---

### Task 5: Скрипты установки, plist-шаблоны, документация, вынос данных из репо

**Files:**
- Modify: `launchd/ru.slesarev.transcriber.app.plist`, `launchd/ru.slesarev.transcriber.worker.plist`, `scripts/build-app.sh:12-30,52`, `scripts/install-worker.sh`, `README.md`, `docs/specs/2026-09-15-transcriber-design.md` (пометка о переезде рядом со схемой каталога `~/Claude/transcriber/`, строка 27), `docs/product/integrations.md`, `docs/agents/architecture.md`
- Delete (git rm): `docs/quality-log.md`, `docs/plans/2026-10-14-quality-backlog.md`, `docs/lavish/2026-10-14-before-after.html` (переезжают в каталог данных при миграции — контроллер копирует их из main **до** мержа)

**Interfaces:**
- Consumes: `transcriber init` (Task 2), `AppPaths` (Task 3), меню «Настройки…» (Task 4).
- Produces: plist-файлы — шаблоны с плейсхолдерами `__HOME__` (вместо абсолютного пути домашнего каталога) и `__REPO__` (вместо абсолютного пути к репо); оба скрипта ставят их через `sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$ROOT|g" "$SRC" > "$DST"`; `DATA_DIR="${TRANSCRIBER_DATA:-$HOME/Library/Application Support/Transcriber}"` в обоих скриптах; `build-app.sh` проверяет незавершённые записи в `$DATA_DIR/sessions`; `install-worker.sh` после установки печатает подсказку `каталог данных не настроен: cd worker && uv run transcriber init`, если нет `$DATA_DIR/config.yaml`.

- [ ] **Step 1: plist-шаблоны и скрипты** по Interfaces (`ROOT` в `install-worker.sh` вычислить как в `build-app.sh`). Проверка: `bash -n scripts/*.sh`; `sed`-подстановка в `/tmp`-файл и `plutil -lint` на результате — OK; `grep -c "__HOME__\|__REPO__" <результат>` = 0.
- [ ] **Step 2: README** — переписать: «Установка» (1. `scripts/install-worker.sh`; 2. `cd worker && uv run transcriber init`; 3. `scripts/build-app.sh`; 4. разрешения macOS), «Где лежат данные» — таблица из спеки (файл → назначение), `TRANSCRIBER_DATA`, «Настройки…» в меню и что пороги — только в `config.yaml`; «Установка под себя» — оставить про голоса и словарь, пути — в каталог данных; явная фраза: пока хосты встреч не вписаны, звонок в браузере не считается рабочим и попадает под окна приватности.
- [ ] **Step 3: Документы**: в `docs/specs/2026-09-15-transcriber-design.md` над схемой каталога — абзац «С 14.10.2026 (тикет 12) данные установки — `sessions/`, `voices/`, `bench/`, `config.yaml`, `app-config.json`, `glossary.txt`, лог и бэклог качества — живут в `~/Library/Application Support/Transcriber/`; схема ниже историческая, см. `docs/specs/2026-10-14-data-dir-separation.md`»; `docs/product/integrations.md`: строки `quality-log.md` и `voices/` — новые пути; новая строка `app-config.json` + `config.yaml` в каталоге данных — пишут `transcriber init` и меню «Настройки…», читают воркер и приложение; новая строка «launchd-агенты `~/Library/LaunchAgents/ru.slesarev.transcriber.*.plist` — пишут `scripts/*.sh` из шаблонов, читает launchd».
- [ ] **Step 4: `git rm`** трёх файлов данных; `ls docs/lavish` пуст → удалить каталог.
- [ ] **Step 5: Карта** (`scripts/`, `launchd/` как шаблоны), **коммит** `install: plist-шаблоны и каталог данных в скриптах; README про установку и данные; лог и бэклог качества уезжают из репо`.

---

### Task 6: Вычистка имён и хостов, финальный гейт

**Files:**
- Modify: все файлы, которые находит `git grep -n -i -f <список>` (список стоп-слов контроллер передаёт файлом вне репо; его **нельзя** копировать в репо); ожидаемо: `worker/tests/*.py`, `worker/transcriber/cli.py` (примеры в докстрингах и `help`), `worker/transcriber/talk.py`, `worker/transcriber/voices.py`, `app/Tests/TranscriberCoreTests/*.swift`, `app/Tests/TranscriberCoreTests/Fixtures/talk-grid.txt`, `app/Sources/TranscriberCore/BrowserTabs.swift`, `docs/specs/*.md`, `docs/plans/*.md`, `spike/whisperkit/*.py`.

**Interfaces:** нет новых. Замены — по Global Constraints (вымышленные имена, `talk.example.com`, окно `3 12:00–12:50`, «Еженедельный синк», «Переговорная 5», «приватная встреча»); слова компании в тестовом глоссарии (`test_quality.py`, `test_cli.py`, `test_pipeline.py`) → «Ромашка», «Омни», «CDP»; названия встреч и компаний в документах → обобщить («встреча с подрядчиком», «ревью документа»). В фикстуре `talk-grid.txt` имена заменить **согласованно** с ожиданиями `TalkObserverTests.swift`/`TalkInspectorTests.swift`; длину и структуру дампа не менять. Строка-пример `transcriber learn SESSION_ID "Speaker 2=Анна Петрова"`.

- [ ] **Step 1: Проверка до** — `git grep -n -i -f <список> | wc -l` (ожидается ≈ 300 строк без уже удалённых файлов).
- [ ] **Step 2: Замены** файл за файлом; не менять семантику тестов (число участников, порядок, пороги).
- [ ] **Step 3: Гейт** — `git grep -i -f <список>` пуст; `git grep -i "Владислав" -- app/Sources app/Tests worker spike` пуст (в `docs/` имя владельца допустимо).
- [ ] **Step 4: Оба набора тестов** — PASS, числа не меньше, чем после Task 4.
- [ ] **Step 5: Коммит** `docs/tests: вымышленные имена и хосты вместо реальных`.
