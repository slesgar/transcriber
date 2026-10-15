# Владелец установки и его голосовой профиль — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Имя владельца и его голосовой профиль становятся настройкой установки; канал микрофона диаризуется, и чужие голоса в переговорной перестают подписываться `Me`.

**Architecture:** Четыре шага поверх существующего воркера: (1) `owner_*` в `Config`, зашитое «Владислав» уходит из `asr`/`quality`/`vault`; (2) реплики микрофона получают эмбеддинги в отдельные списки `State.mic_embeddings`/`mic_cluster_of_emb`, живая метка остаётся `Me`; (3) новый модуль `owner.py` — автообучение профиля владельца с «чистого» микрофона и перелейблинг mic-кластеров на финализации; (4) шапка Raw и README. Всё через `recluster_records`, поэтому `refinalize` получает ту же логику бесплатно.

**Tech Stack:** Python 3.12 / uv, numpy, scipy, pytest (`cd ~/Claude/transcriber/worker && uv run pytest -q --ignore=tests/test_real_models.py`; сейчас 282 passed). Коммиты из корня репо, сообщение заканчивается `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

**Spec:** `docs/specs/2026-10-14-owner-profile.md`. Отклонение от спеки, закреплённое здесь: `Config` плоский (YAML-ключи = поля dataclass), поэтому вместо блока `owner:` — ключи `owner_name`, `owner_talk_name`, `owner_thoughts_heading`, `owner_autolearn`, `owner_min_samples`.

## Global Constraints

- Умолчания воспроизводят текущую установку байт в байт: `owner_name="Владислав"`, `owner_thoughts_heading="## Мысли Владислава"`, `owner_talk_name=""`. Существующие тесты (`test_vault`, `test_asr`, `test_quality`) должны проходить без правок.
- `state.json` старых сессий без новых полей читается (`_known` уже отбрасывает лишнее; новые поля — с `default_factory=list`/`dict`).
- Приватная сессия: ни чтения библиотеки, ни обучения, все mic-реплики `Me`.
- Живой транскрипт не мигает: во время записи mic-реплика всегда `Me`; перелейблинг только в `recluster_records`.
- Ничего личного в тестах: вымышленный владелец («Анна Петрова») задаётся через `Config`.
- Новые ключи — в `worker/config.yaml` с комментарием в стиле файла.

## Review Focus

1. Реплика микрофона с `text=None` (падение ASR) или пустым текстом (галлюцинация) — эмбеддинг не считается, `emb=None`, метка наследуется у соседа (тест в задаче 2).
2. Реплика микрофона, отброшенная эхо-фильтром, оставляет «сиротский» эмбеддинг в `mic_embeddings`; обучение и перелейблинг считают только эмбеддинги живых записей (тест в задаче 3).
3. Профиль владельца удалён руками (`transcriber forget`) после того, как созрел, — все mic-реплики снова `Me`, ошибок нет (тест в задаче 4).
4. Повторный `refinalize` не удваивает отпечатки владельца: источник `<сессия>:mic` снимается перед записью (тест в задаче 3).
5. Имя владельца с символами, недопустимыми в имени файла (`Анна/Петрова`) — профиль пишется через `safe_filename`, уже есть в `voices.py` (тест в задаче 3).

---

### Task 1: Владелец — настройка

**Files:**
- Modify: `worker/transcriber/config.py` (поля Config, `__post_init__`), `worker/config.yaml`
- Modify: `worker/transcriber/asr.py:22-33` (`build_prompt`), `worker/transcriber/quality.py:33-38` (`prompt_vocabulary`)
- Modify: `worker/transcriber/vault.py:17,74-131` (`render_raw`, `participants_of`, `render_stub`) и их вызовы (`write_raw`, `ensure_stub`, `cli.py` там, где зовутся `build_prompt`/`prompt_vocabulary`; найти `grep -n "build_prompt\|prompt_vocabulary\|participants_of\|render_stub\|render_raw(" worker/transcriber/*.py`)
- Test: `worker/tests/test_config.py`, `worker/tests/test_vault.py`, `worker/tests/test_asr.py`, `worker/tests/test_quality.py`

**Interfaces:**
- Produces: `Config.owner_name: str = "Владислав"`, `Config.owner_talk_name: str = ""`, `Config.owner_thoughts_heading: str = "## Мысли Владислава"`, `Config.owner_autolearn: bool = True`, `Config.owner_min_samples: int = 30`. `Config.__post_init__` добавляет `owner_talk_name` в `talk_shared_names`, если он задан и его там нет.
- `build_prompt(participants, glossary, include_participants=False, owner="Владислав")`; `prompt_vocabulary(participants, glossary, owner="Владислав")`; `participants_of(meta, extra=None, owner="Владислав")`; `render_stub(meta, raw_stem, owner="Владислав", thoughts_heading=THOUGHTS)`; `render_raw(..., owner="Владислав")` — шапка ««Me» — {owner} (микрофон), …». Модульная константа `THOUGHTS` остаётся как умолчание. Все вызовы из `pipeline.py`/`vault.py`/`cli.py` передают значения из `cfg`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_config.py
def test_owner_defaults_reproduce_vladislavs_install():
    cfg = Config()
    assert cfg.owner_name == "Владислав" and cfg.owner_thoughts_heading == "## Мысли Владислава"
    assert cfg.owner_talk_name == "" and cfg.owner_autolearn is True and cfg.owner_min_samples == 30

def test_owner_talk_name_joins_shared_names():
    cfg = Config(owner_talk_name="Анна Петрова", talk_shared_names=["Николай Фомин"])
    assert cfg.talk_shared_names == ["Николай Фомин", "Анна Петрова"]
    assert Config(owner_talk_name="Анна Петрова", talk_shared_names=["Анна Петрова"]).talk_shared_names == ["Анна Петрова"]

# tests/test_vault.py — рядом с существующими тестами заглушки и Raw; meta() из файла
def test_stub_and_raw_use_the_configured_owner():
    body = render_stub(meta(), "Local-x", owner="Анна Петрова", thoughts_heading="## Мысли Анны")
    assert "participants: [Анна Петрова, Иван Сидоров, Мария Иванова]\n" in body and "## Мысли Анны\n" in body
    assert "«Me» — Анна Петрова (микрофон)" in render_raw(meta(), [], 1, "идёт запись", owner="Анна Петрова")

# tests/test_asr.py / tests/test_quality.py
def test_prompt_and_vocabulary_name_the_configured_owner():
    assert build_prompt(["Светлана"], [], include_participants=True, owner="Анна Петрова").startswith("Анна Петрова, Светлана.")
    assert {"анна", "петрова"} <= prompt_vocabulary([], [], owner="Анна Петрова")
```

- [ ] **Step 2: Run** `uv run pytest tests/test_config.py tests/test_vault.py tests/test_asr.py tests/test_quality.py -q` → FAIL (unexpected keyword / TypeError).
- [ ] **Step 3: Implement** поля и `__post_init__` в `Config`; параметр `owner` в четырёх функциях; `ensure_stub`/`write_raw`/`Pipeline._prompt_for`/`cli._refinalize` передают `cfg.owner_name` и `cfg.owner_thoughts_heading`. В `config.yaml` блок:

```yaml
# Владелец установки: как он зовётся в заметках и библиотеке голосов, как подписана его плитка в
# Толке (имён из Толка не получает) и заголовок его секции в MTG-заглушке — вольт и его рутины
# ищут заголовок дословно. Под другого пользователя меняются эти три строки.
owner_name: Владислав
owner_talk_name: Анна Петрова
owner_thoughts_heading: "## Мысли Владислава"
# Голосовой профиль владельца копится сам с «чистых» записей микрофона (owner.py); пока в нём
# меньше owner_min_samples отпечатков, канал микрофона целиком остаётся «Me».
owner_autolearn: true
owner_min_samples: 30
```
и убрать «Анна Петрова» из `talk_shared_names` (теперь приходит из `owner_talk_name`).
- [ ] **Step 4: Run** полный набор → 282 + новые passed.
- [ ] **Step 5: Commit** `worker: владелец установки — настройка, не константа`.

### Task 2: Эмбеддинги канала микрофона

**Files:**
- Modify: `worker/transcriber/session.py:47-58` (`State`), `worker/transcriber/pipeline.py` (`__init__` ~245, `_place` ~404, `finalize`), `worker/transcriber/cli.py:186-190` (`_retranscribe` сброс)
- Test: `worker/tests/test_pipeline.py`, `worker/tests/test_session.py`

**Interfaces:**
- Produces: `State.mic_embeddings: list[list[float]]`, `State.mic_cluster_of_emb: list[int]`, `State.mic_speaker_names: dict[str, str]` (заполняется задачей 4). Для `Rec.channel == "mic"` поле `emb` — индекс в `mic_embeddings`; для `sys` — в `embeddings`, как было. `Pipeline.mic_clusterer: OnlineClusterer` из истории `mic_*`. `Rec.speaker` mic-реплики на живой записи — всегда `"Me"`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_session.py
def test_state_without_mic_fields_loads_with_empty_lists():
    st = State.from_dict({"status": "recording", "records": []})
    assert st.mic_embeddings == [] and st.mic_cluster_of_emb == [] and st.mic_speaker_names == {}

# tests/test_pipeline.py — по образцу test_live_step_writes_raw_and_labels; FakeEmbedder из conftest
def test_mic_utterances_get_their_own_embeddings_and_stay_me_live(vault, tmp_path):
    # mic: два тона по 1 с (440 и 660), sys: тишина
    ...
    assert [r.speaker for r in mic_recs] == ["Me", "Me"]
    assert [r.emb for r in mic_recs] == [0, 1] and len(st.mic_embeddings) == 2 and len(st.mic_cluster_of_emb) == 2
    assert st.embeddings == []                     # sys-список не тронут

def test_mic_record_without_text_has_no_embedding(vault, tmp_path):
    # ASR, падающий на mic (см. test_echo_dropped_and_asr_error_marked) → rec.text is None, rec.emb is None
```

- [ ] **Step 2: Run** → FAIL (`mic_embeddings` отсутствует / `emb` is None).
- [ ] **Step 3: Implement**: поля `State`; в `Pipeline.__init__` — `self.mic_clusterer = OnlineClusterer.from_history(cfg.speaker_similarity, st.mic_embeddings, st.mic_cluster_of_emb)`; в `_place` ветка `mic`: если `rec.text` и `seg is not None` и аудио ≥ `min_embed_s` — `emb = self.embed(audio)`, `idx = self.mic_clusterer.assign(emb)`, дописать в `mic_embeddings`/`mic_cluster_of_emb`, `rec.emb = len(...) - 1`; метка не меняется. `_retranscribe` обнуляет `mic_embeddings`, `mic_cluster_of_emb`, `mic_speaker_names`.
- [ ] **Step 4: Run** полный набор → passed.
- [ ] **Step 5: Commit** `worker: эмбеддинги реплик микрофона в state, живая метка остаётся Me`.

### Task 3: Модуль owner.py — чистый микрофон и обучение профиля

**Files:**
- Create: `worker/transcriber/owner.py`
- Test: `worker/tests/test_owner.py`

**Interfaces:**
- Consumes: `State` из задачи 2; `VoiceLibrary.add/forget_source/get` из `voices.py`; `recluster`, `absorb_small_clusters`, `centroid_of`.
- Produces:
  - `OWNER_SOURCE_SUFFIX = "mic"`; `OWNER_CLEAN_SHARE = 0.8`; `OWNER_KEPT_MIN = 0.17`.
  - `live_mic_embeddings(st: State) -> list[tuple[int, list[float]]]` — пары (индекс, вектор) только для mic-записей из `st.records` с непустым `text`, `emb is not None` и (`kept is None` или `kept >= OWNER_KEPT_MIN`).
  - `clean_mic_embeddings(st: State, similarity: float) -> list[list[float]] | None` — перекластеризация живых mic-эмбеддингов (`recluster` + `absorb_small_clusters`); если доминирующий кластер держит ≥ `OWNER_CLEAN_SHARE` — его векторы, иначе `None`. Меньше двух векторов → `None`.
  - `learn_owner(cfg: Config, st: State, session_id: str, private: bool, library: VoiceLibrary | None = None) -> int` — при `cfg.owner_autolearn and session_id and not private`: `forget_source(owner_name, f"{session_id}:mic")`, затем `add(...)` чистыми векторами; возвращает число добавленных отпечатков (0 — ничего не учили). Лог `voices: %s ← микрофон, +%d отпечатков (всего %d)`.
  - `owner_mature(cfg: Config, library: VoiceLibrary) -> bool` — профиль `cfg.owner_name` есть и `count >= cfg.owner_min_samples`.

- [ ] **Step 1: Failing tests** (`_state_with`-подобный хелпер, `seed_voice` из conftest, `VOICES_DIR` подменяется как в test_pipeline):

```python
def test_clean_mic_is_one_dominant_cluster():          # 9 векторов [1,0,0]-около + 1 вектор [0,1,0] → 9 векторов
def test_mixed_mic_is_not_clean():                      # 5 + 5 разных → None
def test_live_embeddings_skip_empty_text_and_low_kept():# text="" / kept=0.05 / emb=None не входят
def test_learn_owner_writes_profile_and_does_not_duplicate_on_refinalize(tmp_path):
    # дважды learn_owner с одной сессией → count одинаковый, sources == ["2026-10-13-1056:mic"]
def test_learn_owner_skips_private_and_disabled(tmp_path):   # private=True → 0, файла нет; owner_autolearn=False → 0
def test_owner_name_is_a_safe_filename(tmp_path):      # owner_name="Анна/Петрова" → файл существует, profile.name == "Анна/Петрова"
def test_owner_mature_threshold(tmp_path):             # 29 отпечатков → False, 30 → True
```

- [ ] **Step 2: Run** `uv run pytest tests/test_owner.py -q` → FAIL (no module `transcriber.owner`).
- [ ] **Step 3: Implement** `owner.py` по сигнатурам выше.
- [ ] **Step 4: Run** → passed.
- [ ] **Step 5: Commit** `worker: owner.py — чистый микрофон и автообучение профиля владельца`.

### Task 4: Перелейблинг микрофона на финализации, шапка Raw, README

**Files:**
- Modify: `worker/transcriber/owner.py` (добавить `relabel_mic`), `worker/transcriber/pipeline.py:152-215` (`recluster_records`), `worker/transcriber/vault.py:74-108,196-208` (`render_raw`, `write_raw`)
- Create: `README.md` (корень репо) — раздел «Установка под себя»
- Modify: `quality-log.md` / `quality-backlog.md` в каталоге данных — тикет 08 ✅ со строкой стоимости
- Test: `worker/tests/test_owner.py`, `worker/tests/test_pipeline.py`, `worker/tests/test_vault.py`

**Interfaces:**
- `relabel_mic(cfg: Config, st: State, library: VoiceLibrary, taken: set[str], n_sys_clusters: int) -> dict[int, str]`: перекластеризация **всех** `st.mic_embeddings` (`recluster`+`absorb_small_clusters`) → `st.mic_cluster_of_emb`; для кластера с ≥ `cfg.voice_min_samples` живых эмбеддингов — `library.match(centroid)`: владелец → `"Me"`; другое имя не из `taken` → имя; иначе `label(n_sys_clusters + cluster)`. Кластер меньше порога → `"Me"`. Возвращает {кластер: метка}; `st.mic_speaker_names = {str(c): имя}` только для не-`Me` меток. Затем проходит mic-записи по `start`: `emb is not None` → метка кластера, иначе метка предыдущей mic-записи (первая без эмбеддинга — `"Me"`).
- В `recluster_records`: после `name_clusters` и `talk_autolearn` — `learn_owner(...)`; затем если `not private` и `owner_mature(cfg, lib)` — `relabel_mic(cfg, st, lib, taken=set(names.values()), n_sys_clusters=len(set(st.speaker_of_emb)))`; иначе все mic-записи `"Me"` и `st.mic_speaker_names = {}`.
- `render_raw(..., mic_guests: list[str] | None = None)` — в шапку после «Опознаны по голосу»: `«На микрофоне кроме владельца: Speaker 7, Иван Петров. »`; `write_raw` собирает `mic_guests` из меток mic-записей ≠ `Me` по первому появлению.

- [ ] **Step 1: Failing tests**

```python
# tests/test_owner.py
def test_guest_on_the_mic_becomes_speaker_after_sys_clusters(tmp_path):
    # профиль владельца 30 векторов [1,0,0]; mic: 4 записи владельца + 4 гостя [0,1,0]; sys: 2 кластера
    # → гость "Speaker 3", владелец "Me", st.mic_speaker_names == {"1": "Speaker 3"}
def test_known_colleague_on_the_mic_gets_her_name_unless_taken(tmp_path):
    # профиль "Ксения" [0,1,0]; taken=set() → "Ксения"; taken={"Ксения"} → "Speaker N"
def test_immature_owner_keeps_everything_me(tmp_path):          # 29 отпечатков → все Me, mic_speaker_names == {}
def test_owner_profile_deleted_after_maturity_keeps_me(tmp_path): # forget → все Me без исключений
def test_short_mic_record_inherits_previous_mic_label(tmp_path)

# tests/test_pipeline.py
def test_private_session_never_learns_owner_and_keeps_me(vault, tmp_path)   # meta.private=True → voices/ без профиля, все Me
def test_refinalize_path_relabels_mic_via_recluster_records(vault, tmp_path) # recluster_records напрямую со зрелым профилем

# tests/test_vault.py
def test_raw_header_lists_mic_guests():
    assert "На микрофоне кроме владельца: Speaker 3, Ксения." in render_raw(meta(), [], 2, "идёт запись", mic_guests=["Speaker 3", "Ксения"])
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** по интерфейсам. README: установка воркера и приложения уже описаны в `docs/specs/2026-09-15-transcriber-design.md` — README ссылается туда и описывает только «под себя»: три `owner_*` строки, `talk_shared_names`, `glossary.txt`, `app-config.json → privacy`, пустая `voices/`, профиль владельца созреет за 3–5 встреч (`transcriber voices` покажет `count`).
- [ ] **Step 4: Run** полный набор → passed.
- [ ] **Step 5: Commit** `worker: диаризация микрофона — гость в переговорной больше не Me; README «под себя»`.

### Task 5: Стенд на реальных сессиях (ручной шаг координатора, после мержа)

Не задача субагента: нужна рабочая библиотека и аудио. `transcriber refinalize` 4–5 чистых сессий октября (1-1 через Толк: 2026-10-12-1700, 2026-10-13-1300, 2026-10-13-1501) → `transcriber voices` показывает профиль владельца ≥ 30; затем `refinalize 2026-10-13-1056` и `2026-10-13-1746` → доля `Me` и список «На микрофоне кроме владельца» в шапке Raw. Результат — пунктом в `quality-log.md` / `quality-backlog.md` в каталоге данных напротив 13.10 10:56 / 17:46.
