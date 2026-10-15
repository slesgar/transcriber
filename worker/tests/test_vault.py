from datetime import datetime
from transcriber.config import Config
from transcriber.session import Meta, Rec, Session, State
from transcriber.vault import ensure_stub, raw_filename, note_filename, render_raw, slug, write_raw


def meta(title="Синк по CDP / параметры", app="kontur.talk"):
    return Meta(id="2026-09-15-1430", started_at=datetime.fromisoformat("2026-09-15T14:30:00+03:00"),
                app=app, title=title, participants=["Иван Сидоров", "Мария Иванова"])


def test_slug_and_filenames():
    assert slug("Синк по CDP / параметры") == "Синк-по-CDP-параметры"
    assert raw_filename(meta()) == "Local-2026-09-15-1430-Синк-по-CDP-параметры.md"
    assert note_filename(meta()) == "MTG-2026-09-15 1430 · Синк по CDP параметры.md"
    assert raw_filename(meta(title="")) == "Local-2026-09-15-1430-Встреча-в-Толке.md"
    assert note_filename(meta(title="", app="zoom.us")) == "MTG-2026-09-15 1430 · Встреча в Zoom.md"


def test_render_raw_body_and_markers():
    recs = [
        Rec("mic", 0.5, 2.0, "Привет.", "Me", None),
        Rec("sys", 2.1, 4.0, "Привет, слышно?", "Speaker 1", 0),
        Rec("sys", 4.5, 5.0, None, "Speaker 1", None),
        Rec("sys", 301.0, 303.0, "Дальше.", "Speaker 2", 1),
    ]
    text = render_raw(meta(), recs, n_speakers=2, status_text="идёт запись", marker_every_s=300)
    assert text.startswith("---\ntype: raw\nsource: local\nsource_date: 2026-09-15\nprojects: []\ntags: [raw]\n---\n")
    assert "# Синк по CDP / параметры — 2026-09-15 14:30" in text
    assert "Участники: Иван Сидоров, Мария Иванова" in text
    assert "Говорящих на канале собеседников: 2" in text and "Статус: идёт запись" in text
    body = text.split("## Полный транскрипт\n\n")[1]
    assert body.split("\n\n")[0:2] == ["Me: Привет.", "Speaker 1: Привет, слышно?"]
    assert "[не распознано 00:04–00:05]" in body
    assert "— 00:05 —\n\nSpeaker 2: Дальше." in body
    assert body.count("— 00:00 —") == 0


def test_write_raw_and_stub(vault):
    cfg = Config(vault_root=vault)
    s = Session(vault / "s", meta(), State())
    p = write_raw(cfg, s, status_text="идёт запись")
    assert p == vault / "Memory/50 · Raw/Local/Local-2026-09-15-1430-Синк-по-CDP-параметры.md"
    assert p.exists() and s.state.raw_path == str(p)
    n = ensure_stub(cfg, s)
    assert n == vault / "Memory/30 · Meetings/MTG-2026-09-15 1430 · Синк по CDP параметры.md"
    body = n.read_text()
    assert "id: MTG-2026-09-15-1430\n" in body
    assert "participants: [Owner, Иван Сидоров, Мария Иванова]\n" in body
    assert 'transcript: "[[Local-2026-09-15-1430-Синк-по-CDP-параметры]]"\n' in body
    assert "tags: [meeting, stub]\n" in body
    assert "## Мысли владельца\n" in body and "<!-- дистиллят: ожидает дейли-рутин -->" in body
    n.write_text(body + "\nмоя мысль\n")
    assert ensure_stub(cfg, s) == n and "моя мысль" in n.read_text()   # never overwritten


def test_title_of_falls_back_when_title_is_only_separators():
    assert raw_filename(meta(title="///")) == raw_filename(meta(title=""))


def test_filename_title_length_is_capped():
    long_title = "А" * 200
    assert len(raw_filename(meta(title=long_title))) < 150
    assert len(note_filename(meta(title=long_title))) < 150


def test_app_label_and_locative_share_one_source():
    from transcriber.vault import app_label, app_locative

    assert app_label("kontur.talk") == "Толк"
    assert app_locative("kontur.talk") == "Толке"
    assert app_label("unknown") == "" and app_locative("unknown") == ""


def test_paths_are_pinned_after_first_write(vault):
    cfg = Config(vault_root=vault)
    s = Session(vault / "s", meta(), State())
    p1 = write_raw(cfg, s, status_text="идёт запись")
    n1 = ensure_stub(cfg, s)
    s.meta.title = "Переименовали встречу на ходу"      # Толк меняет заголовок посреди записи
    p2 = write_raw(cfg, s, status_text="идёт запись")
    n2 = ensure_stub(cfg, s)
    assert p2 == p1 and n2 == n1
    assert len(list((vault / "Memory/50 · Raw/Local").iterdir())) == 1
    assert len(list((vault / "Memory/30 · Meetings").iterdir())) == 1


def test_n_speakers_counts_sys_speakers_without_embeddings(vault):
    cfg = Config(vault_root=vault)
    st = State(records=[Rec("sys", 0.5, 0.8, "Ага.", "Speaker 1", None),
                        Rec("sys", 1.0, 3.0, "Дальше.", "Speaker 2", 0),
                        Rec("mic", 3.5, 4.0, "Да.", "Me", None)],
               speaker_of_emb=[0])
    s = Session(vault / "s", meta(), st)
    assert "Говорящих на канале собеседников: 2" in write_raw(cfg, s, status_text="идёт запись").read_text()


# ---- приватная запись: мимо вольта ------------------------------------------


def private_meta(**kw):
    m = meta(**kw)
    m.private = True
    return m


def test_private_paths_leave_the_vault(vault, private_dir):
    cfg = Config(vault_root=vault, private_dir=private_dir)
    from transcriber.vault import note_path, raw_path

    assert raw_path(cfg, private_meta()) == private_dir / "2026-09-15-1430/transcript.md"
    assert note_path(cfg, private_meta()) == private_dir / "2026-09-15-1430/notes.md"


def test_private_raw_has_no_frontmatter_but_the_same_body(vault, private_dir):
    recs = [Rec("mic", 0.5, 2.0, "Привет.", "Me", None),
            Rec("sys", 2.1, 4.0, "Привет, слышно?", "Speaker 1", 0)]
    text = render_raw(private_meta(), recs, n_speakers=1, status_text="идёт запись")
    assert not text.startswith("---")
    assert "type: raw" not in text and "tags: [raw]" not in text
    assert text.startswith("# Синк по CDP / параметры — 2026-09-15 14:30")
    assert "Приватная запись. Вне рабочего вольта." in text
    body = text.split("## Полный транскрипт\n\n")[1]
    assert [x.strip() for x in body.split("\n\n")[0:2]] == ["Me: Привет.", "Speaker 1: Привет, слышно?"]


def test_private_session_writes_nothing_into_the_vault(vault, private_dir):
    from tests.conftest import vault_files

    cfg = Config(vault_root=vault, private_dir=private_dir)
    s = Session(vault / "s", private_meta(), State(records=[Rec("mic", 0.5, 2.0, "Привет.", "Me", None)]))
    p = write_raw(cfg, s, status_text="идёт запись")
    n = ensure_stub(cfg, s)
    assert p == private_dir / "2026-09-15-1430/transcript.md" and p.exists()
    assert n == private_dir / "2026-09-15-1430/notes.md" and n.exists()
    assert s.state.raw_path == str(p) and s.state.note_path == str(n)
    assert vault_files(vault) == []


def test_private_notes_are_plain_and_never_overwritten(vault, private_dir):
    cfg = Config(vault_root=vault, private_dir=private_dir)
    s = Session(vault / "s", private_meta(), State())
    n = ensure_stub(cfg, s)
    body = n.read_text()
    assert body == "# Заметки — 2026-09-15 14:30\n\n"
    assert "---" not in body and "[[" not in body and "stub" not in body
    n.write_text(body + "моя мысль\n")
    assert ensure_stub(cfg, s) == n and "моя мысль" in n.read_text()


def test_private_files_are_readable_only_by_the_owner(vault, private_dir):
    cfg = Config(vault_root=vault, private_dir=private_dir)
    s = Session(vault / "s", private_meta(), State())
    p = write_raw(cfg, s, status_text="идёт запись")
    n = ensure_stub(cfg, s)
    assert (p.stat().st_mode & 0o777) == 0o600
    assert (n.stat().st_mode & 0o777) == 0o600
    assert (p.parent.stat().st_mode & 0o777) == 0o700
    assert (private_dir.stat().st_mode & 0o777) == 0o700


def test_private_paths_are_pinned_after_first_write(vault, private_dir):
    cfg = Config(vault_root=vault, private_dir=private_dir)
    s = Session(vault / "s", private_meta(), State())
    p1, n1 = write_raw(cfg, s, status_text="идёт запись"), ensure_stub(cfg, s)
    s.meta.title = "Переименовали на ходу"
    assert write_raw(cfg, s, status_text="идёт запись") == p1
    assert ensure_stub(cfg, s) == n1
    assert len(list((private_dir / "2026-09-15-1430").iterdir())) == 2


# ---- участники из Толка ------------------------------------------------------


def test_render_raw_names_the_talk_participants_separately():
    text = render_raw(meta(), [], n_speakers=1, status_text="идёт запись",
                      talk_participants=["Пётр Кузьмин", "Переговорная 5"])
    assert "Участники: Иван Сидоров, Мария Иванова." in text
    assert "Участники по данным Толка: Пётр Кузьмин, Переговорная 5." in text
    assert "Участники по данным Толка" not in render_raw(meta(), [], 1, "идёт запись")


def _stub_session(vault, thoughts: str = "") -> tuple[Config, Session, "Path"]:
    from pathlib import Path

    cfg = Config(vault_root=vault)
    s = Session(vault / "s", meta(), State())
    write_raw(cfg, s, status_text="идёт запись")
    n = ensure_stub(cfg, s)
    if thoughts:
        n.write_text(n.read_text() + thoughts)
    return cfg, s, Path(n)


def test_stub_participants_are_extended_while_the_stub_tag_is_there(vault):
    from transcriber.vault import update_stub_participants

    cfg, s, n = _stub_session(vault, thoughts="\nуже записанная мысль\n")
    assert update_stub_participants(cfg, s, ["Пётр Кузьмин", "Мария Иванова"]) is True
    body = n.read_text()
    assert "participants: [Owner, Иван Сидоров, Мария Иванова, Пётр Кузьмин]" in body
    assert "**Участники:** Owner, Иван Сидоров, Мария Иванова, Пётр Кузьмин" in body
    assert "уже записанная мысль" in body            # мысли не трогаем
    assert update_stub_participants(cfg, s, ["Пётр Кузьмин", "Мария Иванова"]) is False  # второй раз нечего


def test_stub_participants_are_left_alone_without_the_stub_tag(vault):
    from transcriber.vault import update_stub_participants

    cfg, s, n = _stub_session(vault)
    n.write_text(n.read_text().replace("tags: [meeting, stub]", "tags: [meeting]"))
    before = n.read_text()
    assert update_stub_participants(cfg, s, ["Пётр Кузьмин"]) is False
    assert n.read_text() == before


def test_stub_participants_do_nothing_for_a_private_or_empty_case(vault, private_dir):
    from transcriber.vault import update_stub_participants

    cfg, s, _n = _stub_session(vault)
    assert update_stub_participants(cfg, s, []) is False
    s.meta.private = True
    assert update_stub_participants(cfg, s, ["Пётр Кузьмин"]) is False


def test_stub_tag_is_seen_even_when_the_thoughts_are_written(vault):
    from transcriber.vault import has_stub_tag, is_untouched_stub

    cfg, s, n = _stub_session(vault, thoughts="\nмоя мысль\n")
    text = n.read_text()
    assert has_stub_tag(text) is True and is_untouched_stub(text) is False
    assert has_stub_tag(text.replace("tags: [meeting, stub]", "tags: [meeting]")) is False
    assert has_stub_tag("просто текст без фронт-маттера") is False


def test_untouched_stub_is_told_from_one_with_thoughts(vault):
    from transcriber.vault import is_untouched_stub

    cfg = Config(vault_root=vault)
    s = Session(vault / "s", meta(), State())
    write_raw(cfg, s, status_text="идёт запись")
    stub = ensure_stub(cfg, s).read_text()
    assert is_untouched_stub(stub) is True
    assert is_untouched_stub(stub + "\nмоя мысль\n") is False
    assert is_untouched_stub(stub.replace("tags: [meeting, stub]", "tags: [meeting]")) is False
    assert is_untouched_stub("просто текст без фронт-маттера") is False


def test_stub_and_raw_use_the_configured_owner():
    from transcriber.vault import render_stub
    body = render_stub(meta(), "Local-x", owner="Анна Петрова", thoughts_heading="## Мысли Анны")
    assert "participants: [Анна Петрова, Иван Сидоров, Мария Иванова]\n" in body and "## Мысли Анны\n" in body
    assert "«Me» — Анна Петрова (микрофон)" in render_raw(meta(), [], 1, "идёт запись", owner="Анна Петрова")


def test_untouched_stub_is_recognised_by_the_configured_heading():
    from transcriber.vault import is_untouched_stub, render_stub
    stub = render_stub(meta(), "Local-x", owner="Анна Петрова", thoughts_heading="## Мысли Анны")
    assert is_untouched_stub(stub, thoughts_heading="## Мысли Анны") is True
    assert is_untouched_stub(stub) is False


def test_raw_header_lists_mic_guests():
    assert "На микрофоне кроме владельца: Speaker 3, Ксения." in render_raw(
        meta(), [], 2, "идёт запись", mic_guests=["Speaker 3", "Ксения"])
    assert "На микрофоне кроме" not in render_raw(meta(), [], 2, "идёт запись")


def test_write_raw_collects_mic_guests_in_first_appearance_order(vault):
    cfg = Config(vault_root=vault)
    st = State(records=[Rec("mic", 1.0, 2.0, "Я.", "Me", None),
                        Rec("mic", 3.0, 4.0, "Привет.", "Speaker 3", None),
                        Rec("mic", 5.0, 6.0, "Здесь.", "Ксения", None),
                        Rec("mic", 7.0, 8.0, "Ещё.", "Speaker 3", None)])
    s = Session(vault / "s", meta(), st)
    assert "На микрофоне кроме владельца: Speaker 3, Ксения." in write_raw(cfg, s, status_text="x").read_text()
