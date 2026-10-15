from dataclasses import replace
import json
from pathlib import Path

import numpy as np
from transcriber.asr import Segment
from transcriber.config import Config
from transcriber.pipeline import Pipeline, embeddings_by_cluster, name_clusters, recluster_records
from transcriber.session import Rec, Session, State
from transcriber.voices import VoiceLibrary
from tests.conftest import (FakeAsr, FakeEmbedder, SR, dominant_freq, fake_prob, make_session, one_hot,
                            seed_voice, silence, to_pcm_bytes, tone)


# блок на реплику: тесты про эхо, говорящих и фильтры, а не про размер блока
# синтетические всплески длятся 1–2 с: порог эмбеддинга здесь 1 с, иначе ни одна реплика отпечатка не получит
FAST = dict(block_min_s=0.5, block_gap_s=0.5, block_idle_s=0.5, min_embed_s=1.0)


def write(d: Path, name: str, audio: np.ndarray):
    (d / name).write_bytes(to_pcm_bytes(audio))


def build(vault, sessions, session_id="2026-09-15-1430", asr=None, **cfg_kw):
    cfg_kw.setdefault("min_embed_s", 1.0)   # тоны в тестах короче рабочего порога 2 с
    cfg = Config(vault_root=vault, sessions_dir=sessions, **cfg_kw)
    s = Session.load(sessions / session_id)
    p = Pipeline(cfg, s, asr=asr or FakeAsr(), embed=FakeEmbedder(), prob_factory=lambda: fake_prob)
    return cfg, s, p


def test_live_step_writes_raw_and_labels(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    mic = np.concatenate([silence(0.5), tone(1.0, 440), silence(5.0)])
    sys_ = np.concatenate([silence(2.0), tone(1.5, 880), silence(1.0), tone(1.5, 1200), silence(4.5)])
    write(d, "mic.pcm", mic)
    write(d, "sys.pcm", sys_)
    cfg, s, p = build(vault, tmp_path / "sessions")
    assert p.step() is True
    texts = [(r.speaker, r.text) for r in sorted(s.state.records, key=lambda r: r.start)]
    assert texts == [("Me", "tone440"), ("Speaker 1", "tone880"), ("Speaker 2", "tone1200")]
    assert s.state.mic_offset > 0 and s.state.sys_offset > 0
    raw = Path(s.state.raw_path).read_text()
    assert "Me: tone440" in raw and "Speaker 2: tone1200" in raw and "Статус: идёт запись" in raw
    assert Path(s.state.note_path).exists()
    assert p.step() is False   # nothing new


def test_echo_dropped_and_asr_error_marked(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    both = np.concatenate([silence(0.5), tone(2.0, 440), silence(1.0)])
    write(d, "mic.pcm", both)
    write(d, "sys.pcm", np.concatenate([silence(0.5), tone(2.0, 440), silence(1.0), tone(1.0, 660), silence(1.0)]))
    cfg, s, p = build(vault, tmp_path / "sessions", asr=FakeAsr(fail_on={660}), **FAST)
    p.step()
    recs = sorted(s.state.records, key=lambda r: r.start)
    assert [r.channel for r in recs] == ["sys", "sys"]          # mic echo dropped
    assert recs[1].text is None and len(s.state.errors) == 1
    assert "[не распознано" in Path(s.state.raw_path).read_text()


def test_mic_utterances_get_their_own_embeddings_and_stay_me_live(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", np.concatenate([silence(0.5), tone(1.0, 440), silence(1.0), tone(1.0, 660), silence(5.0)]))
    write(d, "sys.pcm", silence(9.0))
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.step()
    st = s.state
    mic_recs = sorted((r for r in st.records if r.channel == "mic"), key=lambda r: r.start)
    assert [r.speaker for r in mic_recs] == ["Me", "Me"]
    assert [r.emb for r in mic_recs] == [0, 1]
    assert len(st.mic_embeddings) == 2 and len(st.mic_cluster_of_emb) == 2
    assert st.embeddings == []                     # sys-список не тронут


def test_mic_record_without_text_has_no_embedding(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", np.concatenate([silence(0.5), tone(1.0, 660), silence(5.0)]))
    write(d, "sys.pcm", silence(8.0))
    cfg, s, p = build(vault, tmp_path / "sessions", asr=FakeAsr(fail_on={660}), **FAST)
    p.step()
    recs = [r for r in s.state.records if r.channel == "mic"]
    assert len(recs) == 1 and recs[0].text is None and recs[0].emb is None
    assert s.state.mic_embeddings == []


def test_pending_mic_waits_for_sys_then_kept(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", np.concatenate([tone(1.0, 440), silence(1.0)]))
    write(d, "sys.pcm", silence(1.5))                # sys not yet past mic.end + 1 s
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.step()
    assert s.state.records == [] and len(s.state.pending_mic) == 1
    with open(d / "sys.pcm", "ab") as f:
        f.write(to_pcm_bytes(silence(2.0)))
    p.step()
    assert [r.speaker for r in s.state.records] == ["Me"] and s.state.pending_mic == []


def test_resume_after_crash_is_idempotent(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(6.0))
    write(d, "sys.pcm", np.concatenate([tone(1.0, 880), silence(1.0), tone(1.0, 1200), silence(3.0)]))
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.step()
    n = len(s.state.records)
    cfg2, s2, p2 = build(vault, tmp_path / "sessions", **FAST)   # fresh pipeline from state.json
    assert p2.step() is False
    assert len(s2.state.records) == n
    assert p2.clusterer.n_speakers == 2


def test_finalize_reclusters_and_cleans_up(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(8.0))
    write(d, "sys.pcm", np.concatenate([tone(1.5, 880), silence(1.0), tone(1.5, 1200), silence(1.0), tone(1.5, 880), silence(1.5)]))
    cfg, s, p = build(vault, tmp_path / "sessions")
    p.step()
    (d / "STOP").touch()
    p.finalize()
    assert s.state.status == "finalized"
    assert [r.speaker for r in sorted(s.state.records, key=lambda r: r.start)] == ["Speaker 1", "Speaker 2", "Speaker 1"]
    raw = Path(s.state.raw_path).read_text()
    assert "Статус: финализирован" in raw and "Говорящих на канале собеседников: 2" in raw
    assert not (d / "mic.pcm").exists() and not (d / "sys.pcm").exists()
    assert (d / "state.json").exists()


def test_finalize_keeps_audio_when_configured(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(1.0))
    write(d, "sys.pcm", silence(1.0))
    cfg, s, p = build(vault, tmp_path / "sessions", keep_audio=True)
    p.finalize()
    assert (d / "mic.pcm").exists()


def test_finalize_flushes_open_sys_utterance(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(3.0))
    write(d, "sys.pcm", np.concatenate([silence(0.5), tone(2.5, 880)]))   # запись обрывается на речи
    cfg, s, p = build(vault, tmp_path / "sessions", keep_audio=True)
    p.step()
    assert s.state.records == []            # реплика ещё открыта
    p.finalize()
    recs = [r for r in s.state.records if r.channel == "sys"]
    assert len(recs) == 1 and recs[0].text == "tone880"
    assert "tone880" in Path(s.state.raw_path).read_text()
    cfg2, s2, p2 = build(vault, tmp_path / "sessions", keep_audio=True)   # повтор не дублирует
    p2.finalize()
    assert len([r for r in s2.state.records if r.channel == "sys"]) == 1


def test_pending_mic_flushed_without_sys_track(vault, tmp_path):
    """sys.pcm может вовсе не появиться — реплики владельца не должны висеть до финализации."""
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", np.concatenate([silence(0.5), tone(1.0, 440), silence(5.0)]))
    cfg, s, p = build(vault, tmp_path / "sessions")
    p.step()
    assert [(r.channel, r.text) for r in s.state.records] == [("mic", "tone440")]
    assert s.state.pending_mic == []


def test_step_reads_bounded_chunks_and_reports_backlog(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(1.0))
    write(d, "sys.pcm", silence(90.0))
    cfg, s, p = build(vault, tmp_path / "sessions")
    p.step()
    assert s.state.sys_offset <= 30 * SR + 512
    assert p.has_backlog() is True
    for _ in range(3):
        p.step()
    assert p.has_backlog() is False


def test_state_saved_only_when_something_moved(vault, tmp_path, monkeypatch):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(2.0))
    write(d, "sys.pcm", silence(2.0))
    cfg, s, p = build(vault, tmp_path / "sessions")
    p.step()
    calls: list[int] = []
    monkeypatch.setattr(s, "save_state", lambda: calls.append(1))
    assert p.step() is False
    assert calls == []


def test_short_sys_utterance_inherits_first_embedded_label(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(9.0))
    write(d, "sys.pcm", np.concatenate([
        silence(0.2), tone(0.4, 880), silence(1.0),     # короткая — эмбеддинга не будет
        tone(1.5, 1200), silence(1.0),                  # первая с эмбеддингом
        tone(1.5, 880), silence(1.5),
    ]))
    cfg, s, p = build(vault, tmp_path / "sessions")
    p.step()
    (d / "STOP").touch()
    p.finalize()
    recs = sorted([r for r in s.state.records if r.channel == "sys"], key=lambda r: r.start)
    assert len(recs) == 3 and recs[0].emb is None
    first_emb = next(r for r in recs if r.emb is not None)
    assert recs[0].speaker == first_emb.speaker
    assert recs[2].speaker != first_emb.speaker


def test_restart_inside_utterance_produces_one_record(vault, tmp_path):
    sessions = tmp_path / "sessions"
    whole = np.concatenate([silence(0.5), tone(2.0, 880), silence(1.5)])
    ref_dir = make_session(sessions, session_id="ref", title="Эталон")
    write(ref_dir, "mic.pcm", silence(4.0))
    write(ref_dir, "sys.pcm", whole)
    _, ref_s, ref_p = build(vault, sessions, session_id="ref", **FAST)
    ref_p.step()

    d = make_session(sessions, session_id="split", title="С обрывом")
    write(d, "mic.pcm", silence(4.0))
    write(d, "sys.pcm", whole[: int(1.5 * SR)])          # рестарт посреди реплики
    _, s, p = build(vault, sessions, session_id="split", **FAST)
    p.step()
    with open(d / "sys.pcm", "ab") as f:
        f.write(to_pcm_bytes(whole[int(1.5 * SR):]))
    _, s2, p2 = build(vault, sessions, session_id="split", **FAST)   # свежий Pipeline из state.json
    p2.step()

    recs = [r for r in s2.state.records if r.channel == "sys"]
    ref = [r for r in ref_s.state.records if r.channel == "sys"]
    assert len(ref) == 1 and len(recs) == 1
    assert recs[0].text == ref[0].text
    assert abs(recs[0].start - ref[0].start) <= 512 / SR + 1e-6


def test_pending_mic_survives_restart(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", np.concatenate([tone(1.0, 440), silence(1.0)]))
    write(d, "sys.pcm", silence(1.5))
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.step()
    assert len(s.state.pending_mic) == 1
    assert len(json.loads((d / "state.json").read_text())["pending_mic"]) == 1
    with open(d / "sys.pcm", "ab") as f:
        f.write(to_pcm_bytes(silence(2.0)))
    cfg2, s2, p2 = build(vault, tmp_path / "sessions", **FAST)   # свежий Pipeline
    assert len(s2.state.pending_mic) == 1
    p2.step()
    assert [(r.channel, r.text) for r in s2.state.records] == [("mic", "tone440")]
    assert s2.state.pending_mic == []


def test_hallucinated_prompt_and_filler_are_dropped(vault, tmp_path, caplog):
    """Whisper пересказывает промпт и мычит на неразборчивом — в транскрипт это не попадает."""
    d = make_session(tmp_path / "sessions", participants=["Саша Громов"])
    write(d, "mic.pcm", silence(10.0))
    write(d, "sys.pcm", np.concatenate([
        tone(1.5, 880), silence(1.0), tone(1.5, 1200), silence(1.0), tone(1.5, 660), silence(4.5),
    ]))
    texts = {880: "Термины, Саша Громов.", 1200: "Аааа... Ааа...", 660: "Давайте вернёмся к контракту"}
    asr = FakeAsr(texts=texts)
    cfg, s, p = build(vault, tmp_path / "sessions", asr=asr)
    with caplog.at_level("INFO", logger="transcriber"):
        p.step()
    recs = sorted([r for r in s.state.records if r.channel == "sys"], key=lambda r: r.start)
    assert [r.text for r in recs] == ["", "", "Давайте вернёмся к контракту"]
    assert "prompt leak" in caplog.text and "filler" in caplog.text
    assert len(asr.prompts) == 1                      # три реплики ушли в модель одним блоком
    body = Path(s.state.raw_path).read_text().split("## Полный транскрипт", 1)[1]
    assert "Громов" not in body and "Аааа" not in body
    assert body.strip().endswith("Давайте вернёмся к контракту")


def test_asr_failure_is_still_marked_unrecognised_not_dropped(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(5.0))
    write(d, "sys.pcm", np.concatenate([tone(1.5, 880), silence(2.0)]))
    cfg, s, p = build(vault, tmp_path / "sessions", asr=FakeAsr(fail_on={880}), **FAST)
    p.step()
    assert [r.text for r in s.state.records] == [None]
    assert "[не распознано" in Path(s.state.raw_path).read_text()


class NearEmbedder:
    """660 Гц — похожий, но не совпадающий голос: отдельный кластер, который надо подклеить."""

    VECTORS = {880: (1.0, 0.0, 0.0), 1200: (0.0, 1.0, 0.0), 660: (0.25, 0.0, 0.97)}

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        return np.array(self.VECTORS[dominant_freq(audio)], dtype=np.float32)


def test_finalize_absorbs_single_utterance_speaker(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(13.0))
    write(d, "sys.pcm", np.concatenate([
        tone(1.5, 880), silence(1.0), tone(1.5, 1200), silence(1.0),
        tone(1.5, 880), silence(1.0), tone(1.5, 1200), silence(1.0),
        tone(1.5, 660), silence(1.5),
    ]))
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", min_embed_s=1.0)
    s = Session.load(tmp_path / "sessions" / "2026-09-15-1430")
    p = Pipeline(cfg, s, asr=FakeAsr(), embed=NearEmbedder(), prob_factory=lambda: fake_prob)
    p.step()
    (d / "STOP").touch()
    p.finalize()
    recs = sorted([r for r in s.state.records if r.channel == "sys"], key=lambda r: r.start)
    assert [r.speaker for r in recs] == ["Speaker 1", "Speaker 2", "Speaker 1", "Speaker 2", "Speaker 1"]
    assert len(set(s.state.speaker_of_emb)) == 2
    assert "Говорящих на канале собеседников: 2" in Path(s.state.raw_path).read_text()


class LeakyAsr:
    """Whisper с промптом пересказывает промпт, без промпта слышит живую речь."""

    def __init__(self, always_leaks: bool = False):
        self.always_leaks = always_leaks
        self.prompts: list[str] = []

    def __call__(self, audio: np.ndarray, prompt: str = "") -> list[Segment]:
        self.prompts.append(prompt)
        leak = "Термины, Саша Громов, Ромашка, Омни."
        text = leak if (prompt or self.always_leaks) else "Давайте вернёмся к контракту"
        return [Segment(0.0, len(audio) / SR, text)]


def test_prompt_leak_is_retried_without_prompt(vault, tmp_path, caplog):
    """Утечка промпта — не повод терять реплику: переспрашиваем модель без промпта."""
    d = make_session(tmp_path / "sessions", participants=["Саша Громов"])
    write(d, "mic.pcm", silence(5.0))
    write(d, "sys.pcm", np.concatenate([tone(1.5, 880), silence(2.0)]))
    asr = LeakyAsr()
    cfg, s, p = build(vault, tmp_path / "sessions", asr=asr, glossary=["Ромашка", "Омни", "CDP"], **FAST)
    with caplog.at_level("INFO", logger="transcriber"):
        p.step()
    assert [r.text for r in s.state.records if r.channel == "sys"] == ["Давайте вернёмся к контракту"]
    assert asr.prompts[0] and asr.prompts[1] == ""
    assert "retry without prompt" in caplog.text
    assert "Давайте вернёмся к контракту" in Path(s.state.raw_path).read_text()


def test_prompt_leak_on_retry_too_is_dropped(vault, tmp_path, caplog):
    d = make_session(tmp_path / "sessions", participants=["Саша Громов"])
    write(d, "mic.pcm", silence(5.0))
    write(d, "sys.pcm", np.concatenate([tone(1.5, 880), silence(2.0)]))
    asr = LeakyAsr(always_leaks=True)
    cfg, s, p = build(vault, tmp_path / "sessions", asr=asr, glossary=["Ромашка", "Омни", "CDP"], **FAST)
    with caplog.at_level("INFO", logger="transcriber"):
        p.step()
    assert [r.text for r in s.state.records if r.channel == "sys"] == [""]
    assert asr.prompts == [asr.prompts[0], ""]
    assert "retry without prompt" in caplog.text and "prompt leak" in caplog.text
    assert "Громов" not in Path(s.state.raw_path).read_text().split("## Полный транскрипт", 1)[1]


def test_two_utterances_two_seconds_apart_become_one_block(vault, tmp_path):
    """Пауза в 2 с блок не режет: минимум в 45 с не набран — обе реплики уходят в модель разом."""
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(10.0))
    write(d, "sys.pcm", np.concatenate([silence(0.5), tone(1.5, 880), silence(2.0), tone(1.5, 1200), silence(4.5)]))
    asr = FakeAsr()
    cfg, s, p = build(vault, tmp_path / "sessions", asr=asr)
    assert p.step() is True
    assert len(asr.prompts) == 1                      # один вызов модели на две реплики
    recs = sorted([r for r in s.state.records if r.channel == "sys"], key=lambda r: r.start)
    assert [(r.text, r.speaker) for r in recs] == [("tone880", "Speaker 1"), ("tone1200", "Speaker 2")]
    assert abs(recs[0].start - 0.5) < 0.06 and abs(recs[0].end - 2.0) < 0.06   # времена вернулись в дорожку
    assert abs(recs[1].start - 4.0) < 0.06 and abs(recs[1].end - 5.5) < 0.06


def test_resume_inside_open_block_retranscribes_it_without_duplicates(vault, tmp_path):
    """Падение посреди блока: откатываемся к его началу, переслушиваем один блок, дублей нет."""
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(8.0))
    write(d, "sys.pcm", np.concatenate([silence(0.5), tone(1.5, 880), silence(2.0), tone(1.5, 1200), silence(1.0)]))
    cfg, s, p = build(vault, tmp_path / "sessions", keep_audio=True)
    p.step()
    assert s.state.records == []                                  # блок ещё открыт, текста нет
    assert abs(s.state.sys_offset / SR - 0.4) < 0.06              # откат к началу первой реплики блока
    cfg2, s2, p2 = build(vault, tmp_path / "sessions", keep_audio=True)   # свежий Pipeline из state.json
    p2.finalize()
    recs = sorted([r for r in s2.state.records if r.channel == "sys"], key=lambda r: r.start)
    assert [r.text for r in recs] == ["tone880", "tone1200"]


def test_previous_block_text_is_carried_into_the_next_prompt(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", silence(6.0))
    write(d, "sys.pcm", np.concatenate([tone(1.0, 880), silence(1.0), tone(1.0, 1200), silence(1.0)]))
    asr = FakeAsr()
    cfg, s, p = build(vault, tmp_path / "sessions", asr=asr, **FAST)
    p.step()
    base = p.prompt
    assert asr.prompts == [base, f"{base} tone880"]  # хвост прошлого блока, без разделителей-ярлыков

    asr2 = FakeAsr()
    d2 = make_session(tmp_path / "sessions", session_id="2026-09-15-1500", title="Без контекста")
    write(d2, "mic.pcm", silence(6.0))
    write(d2, "sys.pcm", np.concatenate([tone(1.0, 880), silence(1.0), tone(1.0, 1200), silence(1.0)]))
    _, _, p2 = build(vault, tmp_path / "sessions", session_id="2026-09-15-1500", asr=asr2,
                     carry_context=False, **FAST)
    p2.step()
    assert asr2.prompts == [base, base]


# ---- подавление эха по звуку -------------------------------------------------


ECHO_DELAY = 480          # 30 мс от динамика до микрофона — как на живых записях
ECHO_GAIN = 0.3


def _echo_session(d: Path):
    """Микрофон слышит только динамики — и один раз самого владельца, когда те молчат.

    В начале — тихий широкополосный разгон: его эхо не дотягивает до порога VAD, но
    подавителю хватает, чтобы оценить тракт. Без разгона первые секунды любой записи
    проходят без подавления — подавитель стартует холодным, и это не лечится.
    """
    rng = np.random.default_rng(0)
    warmup = (rng.standard_normal(int(5.0 * SR)) * 0.025).astype(np.float32)
    for i in range(0, len(warmup), int(0.8 * SR)):       # всплесками: по ним ищется задержка
        warmup[i + int(0.5 * SR):i + int(0.8 * SR)] = 0.0
    sys_ = np.concatenate([warmup, tone(2.0, 880), silence(0.8), tone(2.0, 1200), silence(5.0)])
    echo = ECHO_GAIN * np.concatenate([silence(ECHO_DELAY / SR), sys_])[:len(sys_)]
    owner = np.zeros(len(sys_), dtype=np.float32)
    at = int(12.0 * SR)                                  # внутри хвостовой тишины динамиков
    owner[at:at + int(1.5 * SR)] = tone(1.5, 440)
    write(d, "mic.pcm", echo + owner)
    write(d, "sys.pcm", sys_)


def test_echo_suppression_leaves_only_the_owner_on_the_mic(vault, tmp_path, caplog):
    d = make_session(tmp_path / "sessions")
    _echo_session(d)
    cfg, s, p = build(vault, tmp_path / "sessions", echo_suppress=True, keep_audio=True, **FAST)
    with caplog.at_level("INFO", logger="transcriber"):
        p.finalize()
    mic = [r for r in s.state.records if r.channel == "mic"]
    assert [(r.speaker, r.text) for r in mic] == [("Me", "tone440")]
    assert "echo dropped" not in caplog.text          # текстовому фильтру нечего ловить
    assert abs(mic[0].start - 12.0) < 0.2


def test_echo_suppression_off_keeps_the_old_behaviour(vault, tmp_path, caplog):
    """Тот же материал без подавления: эхо доходит до ASR, его вылавливает текстовый фильтр."""
    d = make_session(tmp_path / "sessions")
    _echo_session(d)
    cfg, s, p = build(vault, tmp_path / "sessions", keep_audio=True, **FAST)
    with caplog.at_level("INFO", logger="transcriber"):
        p.finalize()
    assert cfg.echo_suppress is False
    assert [(r.speaker, r.text) for r in s.state.records if r.channel == "mic"] == [("Me", "tone440")]
    assert "echo dropped" in caplog.text


def test_suppression_does_not_stall_when_there_is_no_sys_track(vault, tmp_path):
    """sys.pcm может вовсе не появиться: опору добиваем тишиной, иначе микрофон завис бы."""
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", np.concatenate([silence(0.5), tone(1.0, 440), silence(5.0)]))
    cfg, s, p = build(vault, tmp_path / "sessions", echo_suppress=True, keep_audio=True, **FAST)
    p.step()
    assert [(r.channel, r.text) for r in s.state.records] == [("mic", "tone440")]
    assert p.has_backlog() is False


def test_mic_waits_for_the_sys_track_before_suppression(vault, tmp_path):
    """Живая запись: sys отстаёт от mic — хвост микрофона ждёт опоры, а не идёт вслепую."""
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", np.concatenate([silence(0.5), tone(1.0, 440), silence(5.0)]))
    write(d, "sys.pcm", silence(2.0))
    cfg, s, p = build(vault, tmp_path / "sessions", echo_suppress=True, keep_audio=True, **FAST)
    p.step()
    assert s.state.mic_offset <= 2 * SR                 # дальше опоры микрофон не ушёл
    write(d, "sys.pcm", silence(6.5))
    p.step()
    p.finalize()
    assert [(r.channel, r.text) for r in s.state.records] == [("mic", "tone440")]


GARBLED = "чтобы в вашем сердце это развернулись"


class ResidueAsr(FakeAsr):
    """Тихий всплеск — это остаток эха: whisper сочиняет на нём бессмыслицу, непохожую на sys."""

    def __call__(self, audio, prompt=""):
        out = super().__call__(audio, prompt)
        return [Segment(s.start, s.end, GARBLED)
                if float(np.abs(audio[int(s.start * SR):int(s.end * SR)]).max()) < 0.15 else s
                for s in out]


def _residue_session(d: Path):
    """Эхо, которое подавитель снимает не целиком: динамик ноутбука на громкой речи хрипит.

    Хрип — вторая гармоника, которой в дорожке ``sys`` нет, поэтому вычесть её нечем; прорывается
    она урывками (60 мс из каждых 260). Кадры без хрипа подавитель глушит, с хрипом — пропускает:
    остаток громче порога VAD и сливается в одну реплику, но это считанные проценты энергии
    микрофона. Владелец говорит один раз — в тишине динамиков.
    """
    rng = np.random.default_rng(0)
    warmup = (rng.standard_normal(int(5.0 * SR)) * 0.025).astype(np.float32)
    for i in range(0, len(warmup), int(0.8 * SR)):
        warmup[i + int(0.5 * SR):i + int(0.8 * SR)] = 0.0
    speech = tone(3.0, 880)
    sys_ = np.concatenate([warmup, speech, silence(6.0)])
    echo = ECHO_GAIN * np.concatenate([silence(ECHO_DELAY / SR), sys_])[:len(sys_)]
    rattle = np.zeros(len(sys_), dtype=np.float32)
    harmonic = tone(3.0, 1760, amp=0.05)
    at, on, every = len(warmup) + ECHO_DELAY, int(0.06 * SR), int(0.26 * SR)
    for k in range(0, len(harmonic), every):
        rattle[at + k:at + k + on] = harmonic[k:k + on]
    owner = np.zeros(len(sys_), dtype=np.float32)
    owner[int(10.0 * SR):int(11.5 * SR)] = tone(1.5, 440)
    write(d, "mic.pcm", echo + rattle + owner)
    write(d, "sys.pcm", sys_)


def test_acoustic_residue_dropped_by_kept_energy(vault, tmp_path, caplog):
    d = make_session(tmp_path / "sessions")
    _residue_session(d)
    cfg, s, p = build(vault, tmp_path / "sessions", asr=ResidueAsr(), echo_suppress=True,
                      keep_audio=True, **FAST)
    with caplog.at_level("INFO", logger="transcriber"):
        p.finalize()
    mic = [r for r in s.state.records if r.channel == "mic"]
    assert [(r.speaker, r.text) for r in mic] == [("Me", "tone440")]
    assert mic[0].kept is not None and mic[0].kept > 0.9           # владелец в тишине — нетронут
    assert "echo residue (acoustic) dropped" in caplog.text
    assert GARBLED in caplog.text and "echo dropped" not in caplog.text   # текстовому фильтру не поймать


def test_acoustic_residue_rule_off_keeps_the_residue_with_its_kept(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    _residue_session(d)
    cfg, s, p = build(vault, tmp_path / "sessions", asr=ResidueAsr(), echo_suppress=True,
                      keep_audio=True, echo_kept_max=0.0, **FAST)
    p.finalize()
    mic = sorted((r for r in s.state.records if r.channel == "mic"), key=lambda r: r.start)
    assert [r.text for r in mic] == [GARBLED, "tone440"]
    assert mic[0].kept is not None and mic[0].kept < 0.15


def test_kept_not_computed_without_suppression(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    write(d, "mic.pcm", np.concatenate([silence(0.5), tone(1.0, 440), silence(5.0)]))
    cfg, s, p = build(vault, tmp_path / "sessions")
    p.step()
    assert [r.kept for r in s.state.records] == [None]


# ---- голосовые профили -------------------------------------------------------


def _voiced(d: Path, freq: int = 880, n: int = 3):
    """n реплик одного тона на канале собеседников — один кластер из n эмбеддингов."""
    write(d, "mic.pcm", silence(2.0 * n + 3.0))
    write(d, "sys.pcm", np.concatenate([x for _ in range(n) for x in (tone(1.0, freq), silence(1.0))] + [silence(2.0)]))


def test_finalize_names_a_cluster_from_the_voice_library(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    _voiced(d)
    seed_voice(tmp_path, "Мария Иванова", [one_hot(880)])
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.finalize()
    assert s.state.speaker_names == {"0": "Мария Иванова"}
    assert {r.speaker for r in s.state.records if r.channel == "sys"} == {"Мария Иванова"}
    assert s.state.speaker_of_emb == [0, 0, 0]          # индекс кластера остался доступен
    raw = Path(s.state.raw_path).read_text()
    assert "Опознаны по голосу: Мария Иванова" in raw
    assert "Мария Иванова: tone880" in raw and "Speaker 1:" not in raw


def test_cluster_shorter_than_voice_min_samples_stays_speaker_n(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    _voiced(d, n=2)                                     # два эмбеддинга — меньше порога в три
    seed_voice(tmp_path, "Мария Иванова", [one_hot(880)])
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.finalize()
    assert s.state.speaker_names == {}
    assert {r.speaker for r in s.state.records if r.channel == "sys"} == {"Speaker 1"}
    assert "Опознаны по голосу" not in Path(s.state.raw_path).read_text()


def test_unknown_voice_stays_speaker_n(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    _voiced(d, freq=1200)                               # в библиотеке такого голоса нет
    seed_voice(tmp_path, "Мария Иванова", [one_hot(880)])
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.finalize()
    assert s.state.speaker_names == {}
    assert {r.speaker for r in s.state.records if r.channel == "sys"} == {"Speaker 1"}


def _state_with(embeddings, labels) -> State:
    st = State(embeddings=[[float(x) for x in e] for e in embeddings], speaker_of_emb=list(labels))
    # реплики по 3 с — длиннее рабочего порога min_embed_s, иначе перекластеризация уйдёт в запасную ветку
    st.records = [Rec(channel="sys", start=3.0 * i, end=3.0 * i + 3, text=f"реплика {i}",
                      speaker="Speaker 1", emb=i) for i in range(len(embeddings))]
    return st


def test_short_embeddings_do_not_feed_the_name_centroid(vault, tmp_path):
    """Три длинные реплики Марии и три коротких чужих отпечатка в том же кластере: имя даётся по длинным."""
    seed_voice(tmp_path, "Мария Иванова", [[1.0, 0.0, 0.0]])
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", voice_min_samples=3, min_embed_s=2.0)
    st = _state_with([[1, 0, 0]] * 3 + [[0, 1, 0]] * 3, [0] * 6)
    for r in st.records[3:]:
        r.end = r.start + 1.0                    # короткие: отпечаток шумный, в центроид не идёт
    assert name_clusters(cfg, st) == {0: "Мария Иванова"}
    assert all(len(v) == 3 for v in embeddings_by_cluster(st, cfg.min_embed_s).values())


def test_two_clusters_never_share_one_name(vault, tmp_path):
    """Один профиль — один кластер: побеждает тот, что похож сильнее, второй остаётся Speaker N."""
    seed_voice(tmp_path, "Мария Иванова", [[1.0, 0.0, 0.0]])
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", voice_min_samples=3)
    st = _state_with([[1, 0, 0]] * 3 + [[0.8, 0.6, 0.0]] * 3, [0, 0, 0, 1, 1, 1])
    assert name_clusters(cfg, st) == {0: "Мария Иванова"}

    st = _state_with([[0.8, 0.6, 0.0]] * 3 + [[1, 0, 0]] * 3, [0, 0, 0, 1, 1, 1])
    assert name_clusters(cfg, st) == {1: "Мария Иванова"}      # порядок кластеров ничего не решает


def test_names_survive_a_second_finalization_and_disappear_with_the_profile(vault, tmp_path):
    seed_voice(tmp_path, "Ксения", [[0.0, 1.0, 0.0]])
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", voice_min_samples=3)
    st = _state_with([[0, 1, 0]] * 3, [0, 0, 0])
    recluster_records(cfg, st)
    assert [r.speaker for r in st.records] == ["Ксения"] * 3 and st.speaker_names == {"0": "Ксения"}
    (tmp_path / "voices" / "Ксения.json").unlink()
    recluster_records(cfg, st)                                  # профиль удалили — имя уходит
    assert [r.speaker for r in st.records] == ["Speaker 1"] * 3 and st.speaker_names == {}


# ---- имена из Толка ----------------------------------------------------------


ПЁТР = "Пётр Кузьмин"
КОМНАТА = "Переговорная 5"
БОТ = "KTalk Secretary (саммаризатор встреч)"


def talk_line(t: float, active: str | None, names: list[str]) -> dict:
    return {"t": t, "participants": [{"name": n, "mic": False, "active": n == active} for n in names]}


def write_talk(d: Path, samples: list[dict]) -> Path:
    p = d / "talk.jsonl"
    p.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n", encoding="utf-8")
    return p


def _two_voices(d: Path, first: int = 5, second: int = 3):
    """Два голоса на канале собеседников: ``first`` реплик на 880 Гц, затем ``second`` на 1200."""
    freqs = [880] * first + [1200] * second
    sys_ = np.concatenate([silence(0.5)] + [x for f in freqs for x in (tone(1.0, f), silence(1.0))]
                          + [silence(1.0)])
    write(d, "mic.pcm", silence(len(sys_) / SR + 1.0))
    write(d, "sys.pcm", sys_)


ALL_TILES = [ПЁТР, КОМНАТА, БОТ]


def _talk_session(tmp_path: Path, **kw) -> Path:
    """Пять реплик под плиткой Петра, три — под плиткой переговорки."""
    d = make_session(tmp_path / "sessions", **kw)
    _two_voices(d)
    write_talk(d, [talk_line(0.0, ПЁТР, ALL_TILES), talk_line(10.0, КОМНАТА, ALL_TILES)])
    return d


def test_talk_names_one_cluster_and_leaves_the_room_cluster_alone(vault, tmp_path):
    d = _talk_session(tmp_path)
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.finalize()
    recs = sorted([r for r in s.state.records if r.channel == "sys"], key=lambda r: r.start)
    assert [r.text for r in recs] == ["tone880"] * 5 + ["tone1200"] * 3
    assert [r.talk for r in recs] == [ПЁТР] * 5 + [None] * 3        # переговорка имени не даёт
    assert [r.speaker for r in recs] == [ПЁТР] * 5 + ["Speaker 2"] * 3
    assert s.state.speaker_names == {"0": ПЁТР}
    raw = Path(s.state.raw_path).read_text()
    assert f"{ПЁТР}: tone880" in raw and "Speaker 2: tone1200" in raw
    assert f"Участники по данным Толка: {ПЁТР}, {КОМНАТА}." in raw   # бот в участники не идёт
    assert "Секретарь" not in raw and "KTalk" not in raw


def test_talk_name_survives_a_restart_in_state_json(vault, tmp_path):
    d = _talk_session(tmp_path)
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.step()
    saved = json.loads((d / "state.json").read_text())["records"]
    assert [r["talk"] for r in saved if r["channel"] == "sys"][:3] == [ПЁТР] * 3


def test_talk_cluster_goes_into_the_voice_library_by_itself(vault, tmp_path):
    d = _talk_session(tmp_path)
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.finalize()
    prof = VoiceLibrary(cfg.voices_dir).get(ПЁТР)
    assert prof is not None and prof.count == 5              # пять отпечатков кластера 880 Гц
    assert prof.sources == ["2026-09-15-1430:talk"]
    assert VoiceLibrary(cfg.voices_dir).names() == [ПЁТР]     # переговорка профиля не заводит


def test_autolearn_can_be_turned_off_without_losing_the_name(vault, tmp_path):
    d = _talk_session(tmp_path)
    cfg, s, p = build(vault, tmp_path / "sessions", talk_autolearn=False, **FAST)
    p.finalize()
    assert s.state.speaker_names == {"0": ПЁТР}
    assert VoiceLibrary(cfg.voices_dir).names() == []


def test_a_short_talk_cluster_names_but_does_not_teach(vault, tmp_path):
    """Три реплики — имя в транскрипте есть, в библиотеке нет: порог обучения выше."""
    d = make_session(tmp_path / "sessions")
    _two_voices(d, first=3, second=3)
    write_talk(d, [talk_line(0.0, ПЁТР, ALL_TILES), talk_line(6.0, КОМНАТА, ALL_TILES)])
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.finalize()
    assert s.state.speaker_names == {"0": ПЁТР}
    assert VoiceLibrary(cfg.voices_dir).names() == []


def test_talk_beats_the_voice_library(vault, tmp_path):
    """Библиотека узнала в голосе одного, Толк показывает другого — верим интерфейсу встречи."""
    d = _talk_session(tmp_path)
    seed_voice(tmp_path, "Мария Иванова", [one_hot(880)])
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.finalize()
    assert s.state.speaker_names == {"0": ПЁТР}
    assert "Мария Иванова" not in Path(s.state.raw_path).read_text()


def test_talk_stub_gets_the_participants_while_it_is_still_a_stub(vault, tmp_path):
    d = _talk_session(tmp_path)
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    note = Path(s.state.note_path)
    assert "participants: [Owner, Иван Сидоров]" in note.read_text()
    note.write_text(note.read_text().replace("<!-- дистиллят: ожидает дейли-рутин -->", "уже записал мысль"))
    p.finalize()
    body = note.read_text()
    assert f"participants: [Owner, Иван Сидоров, {ПЁТР}, {КОМНАТА}]" in body
    assert f"**Участники:** Owner, Иван Сидоров, {ПЁТР}, {КОМНАТА}" in body
    assert "уже записал мысль" in body          # мысли под заголовком не мешают дописать шапку


def test_stub_participants_are_not_touched_once_the_tag_is_gone(vault, tmp_path):
    d = _talk_session(tmp_path)
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    note = Path(s.state.note_path)
    note.write_text(note.read_text().replace("tags: [meeting, stub]", "tags: [meeting]"))
    before = note.read_text()
    p.finalize()
    assert note.read_text() == before           # заметку ведёт человек — участников не правим


def _talk_state(records: list[tuple[float, float, str | None]], labels: list[int]) -> State:
    st = State(embeddings=[[1.0, 0.0, 0.0]] * len(records), speaker_of_emb=list(labels))
    st.records = [Rec(channel="sys", start=a, end=b, text=f"реплика {i}", speaker="Speaker 1",
                      emb=i, talk=name) for i, (a, b, name) in enumerate(records)]
    return st


def test_the_vote_needs_three_records_and_agreement(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    st = _talk_state([(0.0, 2.0, ПЁТР), (3.0, 5.0, ПЁТР)], [0, 0])
    recluster_records(cfg, st)
    assert st.speaker_names == {}                       # двух названных реплик мало

    st = _talk_state([(0.0, 2.0, ПЁТР), (3.0, 5.0, "Ксения"), (6.0, 8.0, "Иван Сидоров")], [0, 0, 0])
    recluster_records(cfg, st)
    assert st.speaker_names == {}                       # имена разошлись — кластер без имени


TIMELINE = [(0.0, 10.0, ПЁТР), (10.0, 14.0, "Ксения")]


def test_a_record_covered_whole_keeps_its_own_talk_name(vault, tmp_path):
    """Кластер назван большинством, но одна реплика целиком под чужой плиткой — она её и берёт."""
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", talk_autolearn=False)
    st = _talk_state([(0.0, 4.0, None), (4.0, 8.0, None), (8.0, 10.0, None), (10.5, 12.5, None)],
                     [0, 0, 0, 0])
    recluster_records(cfg, st, timeline=TIMELINE)
    assert [r.talk for r in st.records] == [ПЁТР, ПЁТР, ПЁТР, "Ксения"]   # имена пересчитаны по ленте
    assert st.speaker_names == {"0": ПЁТР}
    assert [r.speaker for r in st.records] == [ПЁТР, ПЁТР, ПЁТР, "Ксения"]


def test_a_partly_covered_record_keeps_the_cluster_name(vault, tmp_path):
    """Плитка накрыла реплику меньше чем на 0.9 — метка кластера сильнее, иначе она бы мигала."""
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", talk_autolearn=False)
    st = _talk_state([(0.0, 4.0, None), (4.0, 8.0, None), (8.0, 10.0, None), (9.5, 12.5, None)],
                     [0, 0, 0, 0])
    recluster_records(cfg, st, timeline=TIMELINE)
    assert [r.talk for r in st.records] == [ПЁТР, ПЁТР, ПЁТР, "Ксения"]   # 2.5 с из 3 — меньше 0.9
    assert [r.speaker for r in st.records] == [ПЁТР] * 4


def test_a_missing_talk_file_changes_nothing(vault, tmp_path):
    d = make_session(tmp_path / "sessions")
    _two_voices(d, first=3, second=3)
    cfg, s, p = build(vault, tmp_path / "sessions", **FAST)
    p.finalize()
    assert s.state.speaker_names == {}
    assert {r.speaker for r in s.state.records if r.channel == "sys"} == {"Speaker 1", "Speaker 2"}
    assert "Участники по данным Толка" not in Path(s.state.raw_path).read_text()


# ---- приватная запись --------------------------------------------------------


class GuardedLibrary:
    """Библиотека, которую нельзя читать: приватная сессия не должна к ней прикасаться."""

    def names(self):
        raise AssertionError("библиотека голосов не читается на приватной сессии")

    def match(self, centroid):
        raise AssertionError("библиотека голосов не читается на приватной сессии")


def test_private_session_keeps_speaker_n_and_never_reads_the_library(vault, tmp_path, private_dir):
    from tests.conftest import vault_files

    d = make_session(tmp_path / "sessions", private=True)
    _voiced(d)
    seed_voice(tmp_path, "Мария Иванова", [one_hot(880)])
    cfg, s, p = build(vault, tmp_path / "sessions", private_dir=private_dir, **FAST)
    p.finalize()
    assert s.state.speaker_names == {}
    assert {r.speaker for r in s.state.records if r.channel == "sys"} == {"Speaker 1"}
    raw = Path(s.state.raw_path)
    assert raw == private_dir / "2026-09-15-1430/transcript.md"
    assert "Опознаны по голосу" not in raw.read_text()
    assert vault_files(vault) == []


def test_private_session_ignores_the_talk_file_entirely(vault, tmp_path, private_dir):
    """Плитки Толка у приватной записи не спрашиваются: ни имён, ни участников, ни библиотеки."""
    from tests.conftest import vault_files

    d = _talk_session(tmp_path, private=True)
    cfg, s, p = build(vault, tmp_path / "sessions", private_dir=private_dir, **FAST)
    p.finalize()
    assert s.state.speaker_names == {}
    assert {r.speaker for r in s.state.records if r.channel == "sys"} == {"Speaker 1", "Speaker 2"}
    assert all(r.talk is None for r in s.state.records)
    assert VoiceLibrary(cfg.voices_dir).names() == []
    text = Path(s.state.raw_path).read_text()
    assert ПЁТР not in text and "Участники по данным Толка" not in text
    assert vault_files(vault) == []


def test_recluster_of_a_private_session_does_not_touch_the_library(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", voice_min_samples=3)
    st = _state_with([[0, 1, 0]] * 3, [0, 0, 0])
    recluster_records(cfg, st, library=GuardedLibrary(), private=True)
    assert [r.speaker for r in st.records] == ["Speaker 1"] * 3 and st.speaker_names == {}


# ---- голос владельца на микрофоне --------------------------------------------


def _mic_only_state(vectors) -> State:
    st = State(mic_embeddings=[list(v) for v in vectors])
    st.records = [Rec(channel="mic", start=float(i), end=float(i) + 1, text=f"реплика {i}",
                      speaker="Me", emb=i) for i in range(len(vectors))]
    return st


def test_private_session_never_learns_owner_and_keeps_me(vault, tmp_path):
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions")
    st = _mic_only_state([[1.0, 0.01 * i, 0.0] for i in range(40)])
    recluster_records(cfg, st, private=True, session_id="2026-09-15-1430")
    assert not list((tmp_path / "voices").glob("*.json"))
    assert {r.speaker for r in st.records} == {"Me"} and st.mic_speaker_names == {}


def test_refinalize_path_relabels_mic_via_recluster_records(vault, tmp_path):
    """refinalize зовёт тот же recluster_records: зрелый профиль → гость на микрофоне перестаёт быть Me."""
    seed_voice(tmp_path, "Анна Петрова", [[1.0, 0.01 * i, 0.0] for i in range(30)])
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", owner_name="Анна Петрова")
    st = _mic_only_state([[1.0, 0.01 * i, 0.0] for i in range(4)] + [[0.01 * i, 1.0, 0.0] for i in range(4)])
    recluster_records(cfg, st, session_id="2026-09-15-1430")
    assert [r.speaker for r in st.records] == ["Me"] * 4 + ["Speaker 1"] * 4
    assert st.mic_speaker_names == {"1": "Speaker 1"}


def _state_with_a_short_misfit(tmp_path):
    """Анна (тон 100) и Иван (тон 200) говорят по две длинные реплики; затем короткая реплика с тоном Анны
    сразу после Ивана — её отпечаток шумный и «притянет» Анну к Ивану, если его кластеризовать."""
    from transcriber.pipeline import recluster_records

    def rec(start, end, emb):
        return Rec("sys", start, end, "…", "Speaker 1", emb=emb)
    st = State(status="finalized")
    tones = [100, 200, 100, 200, 100]
    starts = [0.0, 4.0, 8.0, 12.0, 16.0]
    for i, (f, t0) in enumerate(zip(tones, starts)):
        st.embeddings.append(one_hot(f))
        st.records.append(rec(t0, t0 + (1.5 if i == 4 else 3.0), i))
    st.speaker_of_emb = [0] * 5
    return st, recluster_records


def test_recluster_records_gives_a_short_utterance_the_neighbours_label(vault, tmp_path):
    st, recluster_records = _state_with_a_short_misfit(tmp_path)
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", min_embed_s=2.0, speaker_similarity=0.4)
    recluster_records(cfg, st)
    labels = [r.speaker for r in st.records]
    assert labels[0] == labels[2] and labels[1] == labels[3] and labels[0] != labels[1]
    assert labels[4] == labels[3]          # короткая — за предыдущей длинной, не за своим отпечатком
    assert len(set(st.speaker_of_emb)) == 2


def test_recluster_records_clusters_short_utterances_when_the_threshold_allows(vault, tmp_path):
    st, recluster_records = _state_with_a_short_misfit(tmp_path)
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", min_embed_s=1.0, speaker_similarity=0.4)
    recluster_records(cfg, st)
    labels = [r.speaker for r in st.records]
    assert labels[4] == labels[0]          # при пороге 1 с короткая кластеризуется по своему тону — с Анной


def test_recluster_records_falls_back_to_all_embeddings_when_too_few_are_long(vault, tmp_path):
    st, recluster_records = _state_with_a_short_misfit(tmp_path)
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", min_embed_s=10.0)
    recluster_records(cfg, st)
    assert len(set(st.speaker_of_emb)) == 2    # все короткие — кластеризуем как есть, а не всё в один


def test_recluster_records_splits_a_cluster_shared_by_two_talk_names(vault, tmp_path):
    """Анна и Иван с неразличимыми голосами (один тон) — один кластер; плитки Толка их разводят."""
    from transcriber.pipeline import recluster_records
    st = State(status="finalized")
    for i in range(8):
        st.embeddings.append(one_hot(100))                       # голоса не различимы
        st.records.append(Rec("sys", 3.0 * i, 3.0 * i + 2.5, "…", "Speaker 1", emb=i))
    st.speaker_of_emb = [0] * 8
    timeline = [(3.0 * i, 3.0 * i + 3.0, "Анна Петрова" if i % 2 == 0 else "Иван Сидоров") for i in range(8)]
    cfg = Config(vault_root=vault, sessions_dir=tmp_path / "sessions", talk_autolearn=False)
    recluster_records(cfg, st, timeline=timeline)
    assert len(set(st.speaker_of_emb)) == 2
    assert [r.speaker for r in st.records] == ["Анна Петрова", "Иван Сидоров"] * 4

    st.speaker_of_emb = [0] * 8
    recluster_records(replace(cfg, talk_split_share=0.0), st, timeline=timeline)
    assert len(set(st.speaker_of_emb)) == 1                       # выключатель: кластер остаётся одним
