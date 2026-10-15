from __future__ import annotations

from transcriber.config import Config
from transcriber.owner import (clean_mic_embeddings, learn_owner, live_mic_embeddings, owner_mature,
                               relabel_mic)
from transcriber.pipeline import recluster_records
from transcriber.session import Rec, State
from transcriber.voices import VoiceLibrary

ВЛАДЕЛЕЦ = "Анна Петрова"
СЕССИЯ = "2026-10-13-1056"


def _near(i: int) -> list[float]:
    return [1.0, 0.01 * i, 0.0]          # плотное облако около [1,0,0]


def _mic_state(vectors, **rec_kw) -> State:
    st = State(mic_embeddings=[list(v) for v in vectors])
    st.records = [Rec(channel="mic", start=float(i), end=float(i) + 1, text=f"реплика {i}",
                      speaker="Me", emb=i, **rec_kw) for i in range(len(vectors))]
    return st


def _cfg(tmp_path, **kw) -> Config:
    return Config(vault_root=tmp_path / "vault", sessions_dir=tmp_path / "sessions",
                  voices_dir=tmp_path / "voices", owner_name=ВЛАДЕЛЕЦ, **kw)


def test_clean_mic_is_one_dominant_cluster():
    st = _mic_state([_near(i) for i in range(9)] + [[0.0, 1.0, 0.0]])
    clean = clean_mic_embeddings(st, 0.5)
    assert clean is not None and len(clean) == 9
    assert all(v[0] == 1.0 for v in clean)


def test_mixed_mic_is_not_clean():
    st = _mic_state([_near(i) for i in range(5)] + [[0.0, 1.0, 0.01 * i] for i in range(5)])
    assert clean_mic_embeddings(st, 0.5) is None


def test_live_embeddings_skip_empty_text_and_low_kept():
    st = _mic_state([_near(i) for i in range(6)])
    st.records[1].text = ""                 # галлюцинация отброшена
    st.records[2].kept = 0.05               # почти одно эхо
    st.records[3].emb = None                # вектора нет
    st.records[4].kept = 0.9
    st.records[5].channel = "sys"           # чужой канал
    assert [i for i, _ in live_mic_embeddings(st)] == [0, 4]


def test_learn_owner_writes_profile_and_does_not_duplicate_on_refinalize(tmp_path):
    cfg, st = _cfg(tmp_path), _mic_state([_near(i) for i in range(10)])
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    assert learn_owner(cfg, st, СЕССИЯ, False, lib) == 10
    first = lib.get(ВЛАДЕЛЕЦ).count
    assert learn_owner(cfg, st, СЕССИЯ, False, lib) == 10
    prof = lib.get(ВЛАДЕЛЕЦ)
    assert prof.count == first == 10
    assert prof.sources == [f"{СЕССИЯ}:mic"]


def test_learn_owner_skips_private_and_disabled(tmp_path):
    st = _mic_state([_near(i) for i in range(10)])
    cfg = _cfg(tmp_path)
    assert learn_owner(cfg, st, СЕССИЯ, True) == 0
    assert not (tmp_path / "voices").exists() or not list((tmp_path / "voices").glob("*.json"))
    assert learn_owner(_cfg(tmp_path, owner_autolearn=False), st, СЕССИЯ, False) == 0
    assert not (tmp_path / "voices").exists() or not list((tmp_path / "voices").glob("*.json"))


def test_owner_name_is_a_safe_filename(tmp_path):
    cfg = Config(vault_root=tmp_path / "vault", sessions_dir=tmp_path / "sessions",
                 voices_dir=tmp_path / "voices", owner_name="Анна/Петрова")
    st = _mic_state([_near(i) for i in range(10)])
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    assert learn_owner(cfg, st, СЕССИЯ, False, lib) == 10
    assert lib.path_of("Анна/Петрова").exists()
    assert lib.get("Анна/Петрова").name == "Анна/Петрова"


def test_owner_mature_threshold(tmp_path):
    cfg = _cfg(tmp_path)
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    assert not owner_mature(cfg, lib)                       # профиля нет
    lib.add(ВЛАДЕЛЕЦ, [_near(i) for i in range(29)], "a:mic")
    assert not owner_mature(cfg, lib)
    lib.add(ВЛАДЕЛЕЦ, [_near(30)], "b:mic")
    assert owner_mature(cfg, lib)


# ---- перелейблинг микрофона на финализации -----------------------------------

ГОСТЬ = [0.0, 1.0, 0.0]


def _profiled(tmp_path, owner_count: int = 30, **cfg_kw):
    """Конфиг и библиотека с профилем владельца из owner_count отпечатков около [1,0,0]."""
    cfg = _cfg(tmp_path, **cfg_kw)
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    lib.add(ВЛАДЕЛЕЦ, [_near(i) for i in range(owner_count)], "старая:mic")
    return cfg, lib


def _mixed_mic():
    """4 реплики владельца, затем 4 гостя."""
    return _mic_state([_near(i) for i in range(4)] + [[0.01 * i, 1.0, 0.0] for i in range(4)])


def test_guest_on_the_mic_becomes_speaker_after_sys_clusters(tmp_path):
    cfg, lib = _profiled(tmp_path)
    st = _mixed_mic()
    labels = relabel_mic(cfg, st, lib, taken=set(), n_sys_clusters=2)
    assert [r.speaker for r in st.records] == ["Me"] * 4 + ["Speaker 3"] * 4
    assert st.mic_speaker_names == {"1": "Speaker 3"}
    assert labels == {0: "Me", 1: "Speaker 3"}


def test_known_colleague_on_the_mic_gets_her_name_unless_taken(tmp_path):
    cfg, lib = _profiled(tmp_path)
    lib.add("Светлана", [ГОСТЬ] * 5, "старая:Speaker 1")
    st = _mixed_mic()
    relabel_mic(cfg, st, lib, taken=set(), n_sys_clusters=2)
    assert [r.speaker for r in st.records] == ["Me"] * 4 + ["Светлана"] * 4
    assert st.mic_speaker_names == {"1": "Светлана"}
    st = _mixed_mic()
    taken = {"Светлана"}
    relabel_mic(cfg, st, lib, taken=taken, n_sys_clusters=2)     # её уже назвали на канале собеседников
    assert [r.speaker for r in st.records] == ["Me"] * 4 + ["Speaker 3"] * 4


def test_immature_owner_keeps_everything_me(tmp_path):
    cfg, lib = _profiled(tmp_path, owner_count=29)
    st = _mixed_mic()
    assert not owner_mature(cfg, lib)
    # перелейблинг зовёт только зрелый профиль; здесь проверяем, что без него recluster_records не трогает Me
    recluster_records(cfg, st, library=lib, session_id="s-1")
    assert {r.speaker for r in st.records} == {"Me"}
    assert st.mic_speaker_names == {}


def test_owner_profile_deleted_after_maturity_keeps_me(tmp_path):
    cfg, lib = _profiled(tmp_path)
    st = _mixed_mic()
    recluster_records(cfg, st, library=lib, session_id="s-1")
    assert [r.speaker for r in st.records] == ["Me"] * 4 + ["Speaker 1"] * 4     # профиль зрелый — гость виден
    lib.forget(ВЛАДЕЛЕЦ)
    recluster_records(cfg, st, library=lib, session_id="s-1")
    assert {r.speaker for r in st.records} == {"Me"}                           # профиль удалили — снова все Me
    assert st.mic_speaker_names == {}


def test_short_mic_record_inherits_previous_mic_label(tmp_path):
    cfg, lib = _profiled(tmp_path)
    st = _mixed_mic()
    st.records.append(Rec(channel="mic", start=8.0, end=8.5, text="ага", speaker="Me", emb=None))
    st.records.insert(0, Rec(channel="mic", start=-1.0, end=-0.5, text="да", speaker="Speaker 9", emb=None))
    relabel_mic(cfg, st, lib, taken=set(), n_sys_clusters=2)
    by_start = {r.start: r.speaker for r in st.records}
    assert by_start[-1.0] == "Me"                    # первая без эмбеддинга — Me
    assert by_start[8.0] == "Speaker 3"              # наследует гостя


def test_owner_close_to_a_colleague_profile_is_still_me(tmp_path):
    """Зазор match() съедает победителя, если рядом профиль коллеги; владелец от этого гостем не становится."""
    cfg = _cfg(tmp_path)
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    lib.add(ВЛАДЕЛЕЦ, [[1.0, 0.0, 0.0]] * 30, "a:mic")
    lib.add("Светлана", [[0.95, 0.31, 0.0]] * 5, "b:talk")
    st = _mic_state([_near(i) for i in range(6)])
    assert lib.match([1.0, 0.0, 0.0]) is None            # посылка: зазор действительно сработал
    relabel_mic(cfg, st, lib, taken=set(), n_sys_clusters=1)
    assert {r.speaker for r in st.records} == {"Me"}
    assert st.mic_speaker_names == {}


# ---- вклад одной сессии в профиль ----------------------------------------------


def test_one_session_adds_at_most_ten_vectors(tmp_path):
    cfg, st = _cfg(tmp_path), _mic_state([_near(i) for i in range(100)])
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    assert learn_owner(cfg, st, СЕССИЯ, False, lib) == 10
    assert lib.get(ВЛАДЕЛЕЦ).count == 10


def test_mature_profile_ignores_a_session_whose_mic_is_not_the_owner(tmp_path):
    cfg, lib = _profiled(tmp_path)
    before = lib.get(ВЛАДЕЛЕЦ).count
    st = _mic_state([[0.0, 1.0, 0.01 * i] for i in range(8)])      # чистый, но чужой голос
    assert learn_owner(cfg, st, СЕССИЯ, False, lib) == 0
    prof = lib.get(ВЛАДЕЛЕЦ)
    assert prof.count == before and prof.sources == ["старая:mic"]


def test_mature_profile_still_learns_from_the_owner(tmp_path):
    cfg, lib = _profiled(tmp_path)
    st = _mic_state([_near(i) for i in range(8)])
    assert learn_owner(cfg, st, СЕССИЯ, False, lib) == 8


# ---- метка гостя не пересекается с живой меткой sys --------------------------


def test_guest_label_skips_a_live_sys_label_without_sys_embeddings(tmp_path):
    cfg, lib = _profiled(tmp_path)
    st = _mixed_mic()
    st.records.append(Rec(channel="sys", start=20.0, end=21.0, text="привет", speaker="Speaker 1", emb=None))
    recluster_records(cfg, st, library=lib, session_id="s-1")
    assert st.embeddings == []
    assert [r.speaker for r in st.records if r.channel == "mic"] == ["Me"] * 4 + ["Speaker 2"] * 4


# ---- владелец не исчезает с микрофона -----------------------------------------


def test_owner_is_kept_when_no_cluster_reaches_the_profile(tmp_path):
    cfg, lib = _profiled(tmp_path, speaker_similarity=0.6)
    st = _mic_state([[0.0, 1.0, 0.01 * i] for i in range(6)] + [[0.0, 0.01 * i, 1.0] for i in range(4)])
    labels = relabel_mic(cfg, st, lib, taken=set(), n_sys_clusters=0)
    assert sorted(labels.values()) == ["Me", "Speaker 1"]
    me = [c for c, n in labels.items() if n == "Me"][0]
    assert st.mic_speaker_names == {str(c): n for c, n in labels.items() if c != me}
    assert [r.speaker for r in st.records if r.channel == "mic"].count("Me") == 6


def test_owner_fallback_on_dimension_mismatch_keeps_the_largest_cluster(tmp_path):
    cfg = _cfg(tmp_path, speaker_similarity=0.6)
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    lib.add(ВЛАДЕЛЕЦ, [[1.0, 0.0]] * 30, "старая:mic")
    st = _mic_state([[0.0, 1.0, 0.01 * i] for i in range(6)] + [[0.0, 0.01 * i, 1.0] for i in range(4)])
    labels = relabel_mic(cfg, st, lib, taken=set(), n_sys_clusters=0)
    assert [r.speaker for r in st.records[:6]] == ["Me"] * 6
    assert [r.speaker for r in st.records[6:]] == ["Speaker 1"] * 4
    assert sorted(labels.values()) == ["Me", "Speaker 1"]


# ---- сессия до этой ветки (refinalize) ----------------------------------------


def test_refinalize_of_a_pre_branch_session_keeps_the_mic_as_me(tmp_path):
    cfg, lib = _profiled(tmp_path)
    st = State(embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], speaker_of_emb=[0, 1], mic_embeddings=[])
    st.records = [Rec(channel="sys", start=0.0, end=1.0, text="раз", speaker="Speaker 1", emb=0),
                  Rec(channel="sys", start=2.0, end=3.0, text="два", speaker="Speaker 2", emb=1),
                  Rec(channel="mic", start=4.0, end=5.0, text="я", speaker="Me", emb=None),
                  Rec(channel="mic", start=6.0, end=7.0, text="мы", speaker="Me", emb=None)]
    recluster_records(cfg, st, library=lib, session_id="s-1")
    assert [r.speaker for r in st.records if r.channel == "mic"] == ["Me", "Me"]
    assert st.mic_speaker_names == {}
