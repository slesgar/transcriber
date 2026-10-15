from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

import numpy as np

from .aec import EchoSuppressor
from .asr import Segment, build_prompt
from .blocks import Block, BlockBuilder
from .config import Config
from .echo import drop_repeated_residue, is_acoustic_residue, is_echo, is_residue
from .owner import ME, learn_owner, owner_mature, relabel_mic
from .pcm import PcmReader, read_range
from .quality import junk_reason, prompt_vocabulary, strip_leak_prefix
from .session import Rec, Session, State
from .speakers import OnlineClusterer, absorb_small_clusters, label, recluster, speaker_index, split_by_names
from .talk import MIN_SHARE, Span, TalkFile, is_bot, is_room, is_shared, span_share, speaker_for_span
from .vad import Segmenter
from .vault import ensure_stub, update_stub_participants, write_raw
from .voices import VoiceLibrary, centroid_of

log = logging.getLogger("transcriber")

PROMPT_LEAK = "prompt leak"     # имя правила из quality.junk_reason, которое лечится повтором
CARRY_MAX = 200                 # столько символов прошлого блока канала уносим в следующий промпт
KEPT_MIN_ENERGY = 1e-8          # тише — сырой микрофон почти молчит, доля энергии ничего не значит
KEPT_MIN_S = 0.1                # короче — доля по паре кадров STFT, ей верить нельзя

TALK_VOTE_MIN = 3        # меньше названных реплик в кластере — голосовать не на чем
TALK_VOTE_AGREE = 0.7    # столько длительности названных реплик должно приходиться на одно имя
TALK_RECORD_KEEP = 0.9   # реплика спорит с именем своего кластера, только если накрыта целиком
TALK_LEARN_MIN_EMB = 5   # кластер короче в библиотеку не учим: отпечаток ненадёжен
TALK_LEARN_AGREE = 0.8   # ...и голосование должно быть увереннее, чем просто для имени в Raw


def name_clusters(cfg: Config, st: State, library: VoiceLibrary | None = None) -> dict[int, str]:
    """Сверить центроиды кластеров с библиотекой голосов: {индекс кластера: имя}.

    Кластер короче ``voice_min_samples`` не именуем — по одной-двум репликам отпечаток врёт.
    Одно имя достаётся одному кластеру: при споре побеждает большее сходство, проигравший
    остаётся «Speaker N». Ошибиться именем хуже, чем не опознать.
    """
    lib = library if library is not None else VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    st.speaker_names = {}
    if not lib.names():
        return {}
    by_cluster = embeddings_by_cluster(st, cfg.min_embed_s)
    claims: list[tuple[float, int, str]] = []
    for cluster, embs in by_cluster.items():
        if len(embs) < cfg.voice_min_samples:
            continue
        centroid = centroid_of(embs)
        hit = lib.match(centroid) if centroid else None
        if hit is not None:
            claims.append((hit[1], cluster, hit[0]))
    taken: set[str] = set()
    mapping: dict[int, str] = {}
    for similarity, cluster, name in sorted(claims, key=lambda c: (-c[0], c[1])):
        if name in taken:
            log.info("voices: %s уже занят более похожим кластером — %s остаётся без имени",
                     name, label(cluster))
            continue
        taken.add(name)
        mapping[cluster] = name
        log.info("voices: %s → %s (%.3f, %d реплик)", label(cluster), name, similarity,
                 len(by_cluster[cluster]))
    st.speaker_names = {str(c): mapping[c] for c in sorted(mapping)}
    return mapping


def embeddings_by_cluster(st: State, min_embed_s: float = 0.0) -> dict[int, list[list[float]]]:
    """Эмбеддинги по кластерам — только длинные реплики: короткий отпечаток шумный, в центроид
    имени и в библиотеку голосов ему нельзя (метку соседа он при этом сохраняет)."""
    by_cluster: dict[int, list[list[float]]] = {}
    for i in long_embedding_indices(st, min_embed_s):
        if i < len(st.speaker_of_emb):
            by_cluster.setdefault(int(st.speaker_of_emb[i]), []).append(st.embeddings[i])
    return by_cluster


def talk_vote(st: State) -> dict[int, tuple[str, float]]:
    """Голосование именами Толка внутри голосового кластера: {кластер: (имя, согласие)}.

    Голос взвешен длительностью реплики: секунда речи весит секунду, а не «одну реплику», —
    иначе десяток «ага» перебьёт связный монолог. Кластеру нужно не меньше ``TALK_VOTE_MIN``
    названных реплик и согласие от ``TALK_VOTE_AGREE``: Толк подсвечивает по громкости и на
    перебивках ошибается, поэтому решает большинство, а не первое попавшееся имя.

    Два кластера могут законно получить одно имя: голос одного человека иногда расходится на
    два кластера (гарнитура и динамик ноутбука), и это лучше, чем отдать имя одному из них.
    """
    by_cluster: dict[int, list[Rec]] = {}
    for r in st.records:
        if r.channel != "sys" or r.emb is None or not (0 <= r.emb < len(st.speaker_of_emb)):
            continue
        by_cluster.setdefault(int(st.speaker_of_emb[r.emb]), []).append(r)
    out: dict[int, tuple[str, float]] = {}
    for cluster, recs in by_cluster.items():
        weights: dict[str, float] = {}
        named = 0
        for r in recs:
            if not r.talk:
                continue
            named += 1
            weights[r.talk] = weights.get(r.talk, 0.0) + max(0.0, r.end - r.start)
        total = sum(weights.values())
        if named < TALK_VOTE_MIN or total <= 0:
            continue
        name, weight = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        agreement = weight / total
        if agreement < TALK_VOTE_AGREE:
            log.info("talk: %s — имена расходятся (%s, согласие %.2f), остаётся без имени",
                     label(cluster), ", ".join(sorted(weights)), agreement)
            continue
        out[cluster] = (name, agreement)
        log.info("talk: %s → %s (согласие %.2f, %d реплик из %d)", label(cluster), name, agreement,
                 named, len(recs))
    return out


def talk_autolearn(cfg: Config, st: State, votes: dict[int, tuple[str, float]], session_id: str) -> None:
    """Отпечатки кластеров, уверенно названных Толком, — в библиотеку голосов.

    Раньше библиотека пополнялась только руками (``transcriber learn``), и это был весь смысл
    команды; Толк называет говорящего сам, так что учить можно на ходу. Планка выше, чем для
    имени в транскрипте: ошибка в Raw живёт одну встречу, ошибка в библиотеке — все следующие.
    Источник у отпечатков один на сессию (``<сессия>:talk``), и прошлый вклад этой же сессии
    снимается перед записью — повторный ``refinalize`` не должен копить дубли.
    """
    if not (cfg.talk_autolearn and session_id):
        return
    by_cluster = embeddings_by_cluster(st, cfg.min_embed_s)
    by_name: dict[str, list[list[float]]] = {}
    for cluster, (name, agreement) in sorted(votes.items()):
        embs = by_cluster.get(cluster, [])
        if len(embs) < TALK_LEARN_MIN_EMB or agreement < TALK_LEARN_AGREE:
            continue
        if is_room(name, cfg.talk_room_patterns) or is_bot(name, cfg.talk_bot_patterns) or is_shared(name, cfg.talk_shared_names):
            continue                     # такие имена голосование и не выдаёт, но библиотека дороже
        by_name.setdefault(name, []).extend(embs)
    if not by_name:
        return
    source = f"{session_id}:talk"
    lib = VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    for name, embs in by_name.items():
        lib.forget_source(name, source)
        prof = lib.add(name, embs, source=source)
        if prof is not None:
            log.info("voices: %s ← Толк, +%d отпечатков (всего %d)", name, len(embs), prof.count)


def _label_mic(cfg: Config, st: State, lib: VoiceLibrary | None, names: dict[int, str],
               private: bool, session_id: str) -> None:
    """Микрофон: учим профиль владельца, затем — если ему уже можно верить — отделяем гостей.

    Учимся ДО проверки зрелости, чтобы чистое аудио этой же встречи тоже считалось. Приватная
    запись не учит и не читает библиотеку: все mic-реплики остаются ``Me``.
    """
    if lib is not None and not private:
        learn_owner(cfg, st, session_id, private, lib)
        if owner_mature(cfg, lib):
            # метки гостей идут после ВСЕХ живых меток sys: часть sys-реплик может быть без эмбеддинга
            # (короткие), и их «Speaker N» не видны в speaker_of_emb — иначе гость получит чужую метку
            sys_numbers = [n for r in st.records if r.channel == "sys"
                           if (n := speaker_index(r.speaker)) is not None]
            relabel_mic(cfg, st, lib, taken=set(names.values()),
                        n_sys_clusters=max(len(set(st.speaker_of_emb)), *sys_numbers, 0))
            return
    st.mic_speaker_names = {}
    st.mic_cluster_of_emb = []
    for r in st.records:
        if r.channel == "mic":
            r.speaker = ME


def long_embedding_indices(st: State, min_embed_s: float) -> list[int]:
    """Индексы эмбеддингов sys-реплик длиной от ``min_embed_s``; эмбеддинг без реплики считается длинным."""
    short = {r.emb for r in st.records
             if r.channel == "sys" and r.emb is not None and r.end - r.start < min_embed_s}
    return [i for i in range(len(st.embeddings)) if i not in short]


def recluster_embeddings(cfg: Config, st: State) -> list[int]:
    """Перекластеризация всех эмбеддингов сессии текущими порогами — та же, что считает стенд.

    Реплики короче ``min_embed_s`` в кластеризацию не идут: отпечаток с полутора секунд шумный и
    именно он склеивал людей (стенд 14.10: порог 2 с убрал треть расколов и шестую часть склеек).
    У старых сессий такие эмбеддинги уже записаны, поэтому их не удаляем, а даём метку соседа —
    предыдущей длинной реплики, как живой конвейер даёт реплике без эмбеддинга.
    """
    keep = long_embedding_indices(st, cfg.min_embed_s)
    if len(keep) < 2:
        keep = list(range(len(st.embeddings)))
    embs = [st.embeddings[i] for i in keep]
    labels = absorb_small_clusters(recluster(np.asarray(embs, dtype=np.float32), cfg.speaker_similarity), embs)
    known = dict(zip(keep, labels))
    out: list[int] = []
    last = labels[0]
    for i in range(len(st.embeddings)):
        if i in known:
            last = known[i]
        out.append(last)
    return out


def talk_names_by_embedding(st: State, min_embed_s: float) -> list[str | None]:
    """Имя плитки на каждый эмбеддинг (по ``Rec.talk``); короткие реплики имени не дают."""
    names: list[str | None] = [None] * len(st.embeddings)
    long = set(long_embedding_indices(st, min_embed_s))
    for r in st.records:
        if r.channel == "sys" and r.emb is not None and r.emb in long and r.talk:
            names[r.emb] = r.talk
    return names


def split_merged_clusters(cfg: Config, st: State) -> int:
    """Разрезать кластеры, которые Толк делит между людьми (``split_by_names``); вернуть число разрезов."""
    if cfg.talk_split_share <= 0 or len(st.embeddings) < 2 or len(st.speaker_of_emb) != len(st.embeddings):
        return 0
    names = talk_names_by_embedding(st, cfg.min_embed_s)
    before = len(set(st.speaker_of_emb))
    st.speaker_of_emb, splits = split_by_names(st.speaker_of_emb, st.embeddings, names, cfg.talk_split_share,
                                               cfg.talk_split_min_count)
    if splits:
        log.info("talk: разрезано склеенных кластеров: %d (%d → %d)", splits, before, len(set(st.speaker_of_emb)))
    return splits


def drop_repeated_mic_residue(cfg: Config, st: State) -> int:
    """Снять с микрофона повторяющийся короткий мусор поверх динамиков (echo.drop_repeated_residue)."""
    st.records, dropped = drop_repeated_residue(st.records, cfg.echo_kept_min_cover)
    for r in dropped:
        log.info("echo residue (repeated) dropped %.1f-%.1f: %s", r.start, r.end, r.text)
    return len(dropped)


def recluster_records(cfg: Config, st: State, library: VoiceLibrary | None = None,
                      private: bool = False, timeline: list[Span] | None = None,
                      session_id: str | None = None) -> None:
    """Глобальная перекластеризация по всем эмбеддингам сессии + перелейблинг sys-реплик.

    Общая для finalize() и `transcriber refinalize`, поэтому вынесена из класса.
    После перекластеризации кластеры сверяются с библиотекой голосов: опознанные получают
    имя человека вместо «Speaker N», индекс кластера остаётся в ``speaker_of_emb``.

    Имена из Толка идут первыми и бьют библиотеку: интерфейс встречи знает, кто говорит,
    а библиотека только догадывается по отпечатку. Имя, уже занятое голосованием, второму
    кластеру от библиотеки не достаётся.

    У приватной записи имён нет вовсе: библиотека не читается, Толк не спрашивается, все
    остаются «Speaker N» — приватный разговор не должен опознаваться по рабочим голосам,
    попадать в библиотеку и вообще светить именами.
    """
    if len(st.embeddings) >= 2:
        st.speaker_of_emb = recluster_embeddings(cfg, st)
    if private:
        st.speaker_names = {}
    spans: list[Span] = [] if private else list(timeline or [])
    if spans:
        # пересчитываем по всей ленте: во время записи хвост talk.jsonl ещё не был дописан
        for r in st.records:
            if r.channel == "sys":
                r.talk = speaker_for_span(spans, r.start, r.end, MIN_SHARE,
                                          cfg.talk_room_patterns, cfg.talk_bot_patterns, cfg.talk_shared_names)
        split_merged_clusters(cfg, st)
    votes = {} if private else talk_vote(st)
    names: dict[int, str] = {c: name for c, (name, _agreement) in votes.items()}
    lib: VoiceLibrary | None = None
    if not private:
        lib = library if library is not None else VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
        taken = set(names.values())
        for cluster, name in name_clusters(cfg, st, lib).items():
            if cluster not in names and name not in taken:
                names[cluster] = name
        talk_autolearn(cfg, st, votes, session_id or "")
    st.speaker_names = {str(c): names[c] for c in sorted(names)}
    _label_mic(cfg, st, lib, names, private, session_id or "")
    if not st.embeddings:
        return

    def speaker_of(emb: int) -> str:
        cluster = int(st.speaker_of_emb[emb])
        return names.get(cluster) or label(cluster)

    sys_recs = [r for r in sorted(st.records, key=lambda r: r.start) if r.channel == "sys"]
    first_emb = next((r for r in sys_recs if r.emb is not None), None)
    # короткая реплика до первой опознанной наследует её метку, а не «Speaker 1» вслепую
    last = speaker_of(first_emb.emb) if first_emb is not None else label(0)
    for r in sys_recs:
        if r.emb is not None:
            r.speaker = speaker_of(r.emb)
            last = r.speaker
        else:
            r.speaker = last
        # кластер сильнее своей реплики — иначе метки мигали бы посреди монолога; уступаем
        # только реплике, которую Толк накрыл целиком: там ошибиться кластером легче
        if r.talk and r.talk != r.speaker and spans and \
                span_share(spans, r.start, r.end, r.talk) >= TALK_RECORD_KEEP:
            r.speaker = r.talk


Asr = Callable[[np.ndarray, str], list[Segment]]
Embed = Callable[[np.ndarray], np.ndarray]
ProbFactory = Callable[[], Callable[[np.ndarray], float]]


class Pipeline:
    def __init__(self, cfg: Config, session: Session, asr: Asr, embed: Embed, prob_factory: ProbFactory):
        self.cfg = cfg
        self.s = session
        self.asr = asr
        self.embed = embed
        st = session.state
        self.readers = {"mic": PcmReader(session.mic_pcm, st.mic_offset), "sys": PcmReader(session.sys_pcm, st.sys_offset)}
        pad = int(cfg.sample_rate * cfg.speech_pad_ms / 1000)
        self.segs: dict[str, Segmenter] = {}
        for ch, off, pcm in (("mic", st.mic_offset, session.mic_pcm), ("sys", st.sys_offset, session.sys_pcm)):
            # on resume the pad before the offset is already on disk — otherwise the first
            # utterance after a restart starts up to speech_pad_ms late
            preroll = read_range(pcm, off - pad, off) if off > 0 and pad > 0 else None
            self.segs[ch] = Segmenter(prob_factory(), cfg.sample_rate, cfg.vad_threshold, cfg.min_silence_ms,
                                      cfg.speech_pad_ms, cfg.max_utterance_s, start_sample=off, preroll=preroll)
        self.builders: dict[str, BlockBuilder] = {
            ch: BlockBuilder(cfg.sample_rate, cfg.block_min_s, cfg.block_max_s,
                             cfg.block_gap_s, cfg.block_gap_keep_s, cfg.block_idle_s)
            for ch in ("mic", "sys")}
        # подавитель живёт вместе с конвейером: после перезапуска воркера он стартует холодным и
        # первые секунды записи идут без подавления, пока не наберётся статистика тракта
        self.aec = EchoSuppressor(cfg.sample_rate, beta=cfg.echo_beta, floor=cfg.echo_floor,
                                  beta_loud=cfg.echo_beta_loud) if cfg.echo_suppress else None
        self._finalizing = False
        self.carry = {"mic": "", "sys": ""}   # хвост прошлого блока канала; после перезапуска пуст
        self.clusterer = OnlineClusterer.from_history(cfg.speaker_similarity, st.embeddings, st.speaker_of_emb)
        self.mic_clusterer = OnlineClusterer.from_history(cfg.speaker_similarity, st.mic_embeddings,
                                                          st.mic_cluster_of_emb)
        self.prompt = build_prompt(session.meta.participants, cfg.glossary,
                                   include_participants=bool(session.meta.participants), owner=cfg.owner_name)
        self.prompt_words = prompt_vocabulary(session.meta.participants, cfg.glossary, cfg.owner_name)
        self.last_sys_speaker = next((r.speaker for r in reversed(st.records) if r.channel == "sys"), label(0))
        # плитки Толка: файла может не быть вовсе, и он дописывается по ходу встречи
        self.talk = TalkFile(session.dir / "talk.jsonl")
        ensure_stub(cfg, session)
        if st.raw_path is None:
            write_raw(cfg, session, "идёт запись", self.talk_participants())
            session.save_state()

    def talk_participants(self) -> list[str]:
        """Кого Толк показал на встрече. У приватной записи не спрашиваем даже при наличии файла."""
        if self.s.meta.private:
            return []
        return self.talk.participants(self.cfg.talk_bot_patterns)

    def talk_name(self, start: float, end: float) -> str | None:
        if self.s.meta.private:
            return None
        return speaker_for_span(self.talk.timeline, start, end, MIN_SHARE,
                                self.cfg.talk_room_patterns, self.cfg.talk_bot_patterns, self.cfg.talk_shared_names)

    # ---- live -------------------------------------------------------------

    def step(self) -> bool:
        changed = False
        cap = max(1, int(self.cfg.sample_rate * self.cfg.read_chunk_s))
        for ch in ("mic", "sys"):
            audio = self._read(ch, cap)   # бэклог за сутки не тянем в память целиком
            blocks: list[Block] = []
            if audio.size:
                for u in self.segs[ch].feed(audio):
                    blocks.extend(self.builders[ch].add(u))
            # позиция чтения — «сколько времени уже прошло»: по ней блок закрывается по простою
            blocks.extend(self.builders[ch].idle(self.readers[ch].offset))
            for b in blocks:
                self._handle_block(ch, b)
                changed = True
        changed |= self._flush_pending_mic(force=False)
        st = self.s.state
        offsets = (self._resume_point("mic"), self._resume_point("sys"))
        moved = offsets != (st.mic_offset, st.sys_offset)
        st.mic_offset, st.sys_offset = offsets
        if changed:
            write_raw(self.cfg, self.s, "идёт запись", self.talk_participants())
        if changed or moved:
            self.s.save_state()
        return changed

    def has_backlog(self) -> bool:
        """Есть ли непрочитанное аудио — step() читает порциями, демон должен продолжать крутить."""
        return any(r.available() > 0 for r in self.readers.values())

    def _read(self, ch: str, cap: int) -> np.ndarray:
        """Порция канала. Микрофон при включённом подавлении читается только вместе с опорой.

        Опора — тот же отрезок дорожки динамиков. Если sys ещё не дописан (живая запись, mic
        успел раньше), хвост микрофона возвращаем читателю и заберём следующим шагом: пускать
        микрофон через подавитель без опоры нельзя — он примет эхо за речь владельца. На
        финализации (и когда дорожки динамиков нет вовсе) недостающее добиваем тишиной,
        иначе хвост записи завис бы навсегда.
        """
        reader = self.readers[ch]
        if ch != "mic" or self.aec is None:
            return reader.read_all(max_samples=cap)
        start = reader.offset
        audio = reader.read_all(max_samples=cap)
        if not audio.size:
            return audio
        ref = read_range(self.s.sys_pcm, start, start + len(audio))
        if len(ref) < len(audio):
            if self._finalizing or not self.s.sys_pcm.exists():
                ref = np.concatenate([ref, np.zeros(len(audio) - len(ref), dtype=np.float32)])
            else:
                reader.offset = start + len(ref)
                audio = audio[:len(ref)]
                if not audio.size:
                    return audio
        return self.aec.process(audio, ref[:len(audio)])

    def _resume_point(self, ch: str) -> int:
        """Точка продолжения после падения: начало открытого блока, иначе граница сегментатора.

        Заново распознаётся максимум один блок, и дубля не будет: всё, что было до этой точки,
        уже лежит в records.
        """
        open_start = self.builders[ch].open_start
        return open_start if open_start is not None else self.segs[ch].boundary()

    def _sec(self, samples: int) -> float:
        return samples / self.cfg.sample_rate

    def _prompt_for(self, ch: str) -> str:
        """Термины плюс хвост прошлого блока этого канала — штатный способ whisper тянуть контекст."""
        tail = self.carry.get(ch, "")
        if not (self.cfg.carry_context and tail):
            return self.prompt
        return f"{self.prompt} {tail[-CARRY_MAX:]}".strip()

    def _transcribe(self, block: Block, prompt: str) -> list[Segment] | None:
        try:
            return self.asr(block.audio, prompt)
        except Exception as e:  # noqa: BLE001 — any model failure becomes a marker in the transcript
            msg = f"asr failed at {self._sec(block.start):.1f}-{self._sec(block.end):.1f}: {e!r}"
            log.warning(msg)
            self.s.state.errors.append(msg)
            return None

    def _reason(self, text: str | None) -> str | None:
        return junk_reason(text, self.prompt_words, self.cfg.language) if text else None

    def _retry_leaky_block(self, block: Block, segments: list[Segment], prompt: str) -> list[Segment]:
        """Если промпт пересказан в половине сегментов и больше — блок переспрашиваем целиком без промпта.

        Под пересказом обычно живая речь, а решать посегментно нельзя: без промпта модель
        нарежет блок иначе, и склеить две нарезки в одну ленту не выйдет.
        """
        if not (prompt and segments):
            return segments
        leaks = sum(1 for s in segments if self._reason(s.text) == PROMPT_LEAK)
        if leaks * 2 < len(segments):
            return segments
        log.info("retry without prompt %.1f-%.1f: %s", self._sec(block.start), self._sec(block.end),
                 segments[0].text[:60])
        retry = self._transcribe(block, "")
        return retry if retry is not None else segments

    def _handle_block(self, ch: str, block: Block) -> None:
        if ch == "sys":
            self.talk.refresh()     # файл растёт вместе со встречей: перед блоком перечитываем
        prompt = self._prompt_for(ch)
        segments = self._transcribe(block, prompt)
        if segments is None:
            self._place(ch, Rec(channel=ch, start=self._sec(block.start), end=self._sec(block.end),
                                text=None, speaker="Me", emb=None), block, None)
            return
        kept: list[str] = []
        for seg in self._retry_leaky_block(block, segments, prompt):
            start = block.map_to_original(seg.start)
            end = max(start, block.map_to_original(seg.end))
            text = strip_leak_prefix(seg.text)   # приставка-утечка срезается, речь под ней остаётся
            reason = self._reason(text)
            if reason:
                # Галлюцинация — это не «не распознано», а пустая реплика: в транскрипте её нет.
                log.info("dropped (%s) %.1f-%.1f: %s", reason, start, end, text)
                text = ""
            else:
                kept.append(text)
            rec = Rec(channel=ch, start=start, end=end, text=text, speaker="Me", emb=None)
            if ch == "mic" and self.aec is not None:
                rec.kept = self._kept(block, seg, start, end)
            self._place(ch, rec, block, seg)
        if self.cfg.carry_context:
            self.carry[ch] = " ".join(kept)[-CARRY_MAX:]

    def _place(self, ch: str, rec: Rec, block: Block, seg: Segment | None) -> None:
        """Реплику микрофона — в очередь на эхо-проверку, реплику собеседников — с меткой говорящего."""
        st = self.s.state
        if ch == "mic":
            # Эмбеддинг считаем здесь, пока звук блока под рукой: после эхо-проверки его уже нет.
            # Реплика, которую потом отсеет эхо-фильтр, оставляет в списке неиспользуемый вектор — это штатно.
            if rec.text and seg is not None:
                audio = self._segment_audio(block, seg)
                if len(audio) >= int(self.cfg.min_embed_s * self.cfg.sample_rate):
                    emb = self.embed(audio)
                    st.mic_cluster_of_emb.append(self.mic_clusterer.assign(emb))
                    st.mic_embeddings.append([float(x) for x in emb])
                    rec.emb = len(st.mic_embeddings) - 1
            st.pending_mic.append(rec)   # живая метка остаётся «Me»: имена гостям раздаст пост-обработка
            return
        rec.talk = self.talk_name(rec.start, rec.end)
        audio = self._segment_audio(block, seg) if seg is not None else np.zeros(0, dtype=np.float32)
        if len(audio) >= int(self.cfg.min_embed_s * self.cfg.sample_rate):
            emb = self.embed(audio)
            idx = self.clusterer.assign(emb)
            st.embeddings.append([float(x) for x in emb])
            st.speaker_of_emb.append(idx)
            rec.emb = len(st.embeddings) - 1
            rec.speaker = label(idx)
            self.last_sys_speaker = rec.speaker
        else:
            rec.speaker = self.last_sys_speaker
        st.records.append(rec)

    def _kept(self, block: Block, seg: Segment, start: float, end: float) -> float | None:
        """Доля энергии микрофона, которую подавитель эха оставил на отрезке реплики.

        Очищенный звук — из склейки блока (координаты блока), сырой — с диска по исходному
        отрезку. Выход подавителя совпадает со входом по номерам сэмплов, так что сравнивать
        можно напрямую. Слишком короткая или почти беззвучная реплика доли не получает.
        """
        if end - start < KEPT_MIN_S:
            return None
        sr = self.cfg.sample_rate
        raw = read_range(self.s.mic_pcm, int(start * sr), int(end * sr))
        clean = self._segment_audio(block, seg)
        if not raw.size or not clean.size:
            return None
        raw_energy = float(np.mean(raw.astype(np.float64) ** 2))
        if raw_energy < KEPT_MIN_ENERGY:
            return None
        return round(float(np.mean(clean.astype(np.float64) ** 2)) / raw_energy, 4)

    def _segment_audio(self, block: Block, seg: Segment) -> np.ndarray:
        sr = self.cfg.sample_rate
        return block.audio[max(0, int(seg.start * sr)):int(seg.end * sr)]

    def _sys_open_start(self) -> float | None:
        """Начало самого раннего незакрытого куска sys: до него mic-реплику судить об эхе рано."""
        open_starts = [x for x in (self.builders["sys"].open_start, self.segs["sys"].open_start) if x is not None]
        return self._sec(min(open_starts)) if open_starts else None

    def _flush_pending_mic(self, force: bool) -> bool:
        st = self.s.state
        # горизонт — позиция sys-канала; если sys.pcm нет или он отстаёт, ориентируемся на mic,
        # иначе реплики владельца висят в pending до самой финализации
        horizon = max(self._sec(self.segs["sys"].pos), self._sec(self.segs["mic"].pos) - self.cfg.echo_wait_s)
        open_start = self._sys_open_start()
        sys_recs = [r for r in st.records if r.channel == "sys"]
        keep: list[Rec] = []
        changed = False
        for rec in st.pending_mic:
            ready = force or (horizon >= rec.end + self.cfg.echo_wait_s and (open_start is None or open_start > rec.end))
            if not ready:
                keep.append(rec)
                continue
            if is_echo(rec, sys_recs, self.cfg.echo_overlap, self.cfg.echo_similarity,
                       self.cfg.echo_strong_similarity):
                log.info("echo dropped %.1f-%.1f: %s", rec.start, rec.end, rec.text)
            elif is_residue(rec, sys_recs):
                log.info("echo residue dropped %.1f-%.1f: %s", rec.start, rec.end, rec.text)
            elif is_acoustic_residue(rec, sys_recs, self.cfg.echo_kept_max, self.cfg.echo_kept_min_cover):
                log.info("echo residue (acoustic) dropped %.1f-%.1f kept=%.2f: %s",
                         rec.start, rec.end, rec.kept, rec.text)
            else:
                st.records.append(rec)
            changed = True
        st.pending_mic = keep
        return changed

    # ---- finalization -----------------------------------------------------

    def finalize(self) -> None:
        self._finalizing = True                    # опору под хвост микрофона добиваем тишиной
        while self.step() or self.has_backlog():   # step() читает порциями — дочитываем весь хвост
            pass
        if self.aec is not None:
            for u in self.segs["mic"].feed(self.aec.flush()):   # хвост короче окна STFT
                for b in self.builders["mic"].add(u):
                    self._handle_block("mic", b)
        for ch in ("mic", "sys"):
            for u in self.segs[ch].flush():        # recording may have stopped mid-speech
                for b in self.builders[ch].add(u):
                    self._handle_block(ch, b)
            for b in self.builders[ch].flush():    # и недобранный блок уходит в ASR как есть
                self._handle_block(ch, b)
        self.s.state.mic_offset = self._resume_point("mic")   # flushed audio must not be re-read
        self.s.state.sys_offset = self._resume_point("sys")
        self._flush_pending_mic(force=True)
        st = self.s.state
        drop_repeated_mic_residue(self.cfg, st)
        self.talk.refresh()
        recluster_records(self.cfg, st, private=self.s.meta.private, timeline=self.talk.timeline,
                          session_id=self.s.meta.id)
        st.status = "finalized"
        participants = self.talk_participants()
        write_raw(self.cfg, self.s, f"финализирован {datetime.now().strftime('%H:%M')}", participants)
        update_stub_participants(self.cfg, self.s, participants)
        self.s.save_state()
        if not self.cfg.keep_audio:
            for p in (self.s.mic_pcm, self.s.sys_pcm):
                p.unlink(missing_ok=True)
