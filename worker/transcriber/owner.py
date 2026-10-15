"""Голос владельца: автообучение профиля по «чистому» микрофону.

Микрофон — это почти всегда владелец, но не всегда: рядом могут говорить коллеги в переговорке,
а эхо колонок протекает в канал даже после подавителя. Учить профиль на таком смешанном звуке
нельзя — чужой голос в профиле хуже, чем его отсутствие. Поэтому мы берём только «живые»
реплики (не галлюцинации, не одно эхо), перекластеризуем их и учимся лишь тогда, когда один
кластер держит подавляющую долю (``OWNER_CLEAN_SHARE``). Иначе сессия молча пропускается.

Отпечатки метятся источником ``<session_id>:mic`` и перед записью снимаются: повторная
финализация той же сессии заменяет вклад, а не дублирует его.
"""

from __future__ import annotations

import logging
from collections import Counter

import numpy as np

from .config import Config
from .session import State
from .speakers import absorb_small_clusters, label, recluster
from .voices import VoiceLibrary, centroid_of, unit

log = logging.getLogger("transcriber")

OWNER_SOURCE_SUFFIX = "mic"
OWNER_CLEAN_SHARE = 0.8     # доля доминирующего кластера, при которой микрофон считается чистым
OWNER_LEARN_MAX = 10        # вклад одной сессии в профиль: окно в 50 отпечатков должно охватывать ~5 встреч
OWNER_KEPT_MIN = 0.17       # ниже — в записи почти одно эхо, голос микрофона в ней не наш


def live_mic_embeddings(st: State) -> list[tuple[int, list[float]]]:
    """Пары (индекс, вектор) для mic-записей, на которых можно учиться.

    Идём по ``st.records``, а не по ``mic_embeddings``: векторы реплик, снятых как эхо, в списке
    остаются, а из записей уже удалены.
    """
    out: list[tuple[int, list[float]]] = []
    for r in st.records:
        if r.channel != "mic" or not r.text or r.emb is None:
            continue
        if r.kept is not None and r.kept < OWNER_KEPT_MIN:
            continue
        if 0 <= r.emb < len(st.mic_embeddings):
            out.append((r.emb, st.mic_embeddings[r.emb]))
    return out


def clean_mic_embeddings(st: State, similarity: float) -> list[list[float]] | None:
    """Векторы доминирующего кластера микрофона или None, если микрофон «смешанный»."""
    live = live_mic_embeddings(st)
    if len(live) < 2:
        return None
    vectors = [v for _, v in live]
    arr = np.asarray(vectors, dtype=np.float64)
    labels = absorb_small_clusters(recluster(arr, similarity), arr)
    top, n = Counter(labels).most_common(1)[0]
    if n / len(labels) < OWNER_CLEAN_SHARE:
        return None
    return [v for v, l in zip(vectors, labels) if l == top]


def learn_owner(cfg: Config, st: State, session_id: str, private: bool,
                library: VoiceLibrary | None = None) -> int:
    """Дописать чистые отпечатки микрофона в профиль владельца; вернуть, сколько добавлено."""
    if not (cfg.owner_autolearn and session_id and not private):
        return 0
    clean = clean_mic_embeddings(st, cfg.speaker_similarity)
    if not clean:
        return 0          # учить нечего — библиотеку не трогаем, прошлый вклад сессии остаётся
    lib = library if library is not None else VoiceLibrary(cfg.voices_dir, cfg.voice_match_similarity)
    if owner_mature(cfg, lib):
        # зрелому профилю чужой «чистый» микрофон (ноутбук коллеги, гарнитура гостя) не доверяем:
        # одна такая сессия иначе вытеснила бы владельца из окна отпечатков
        sim = _owner_similarity(cfg, lib, centroid_of(clean) or [])
        if sim is None:
            raw = _raw_similarity(cfg, lib, centroid_of(clean) or [])
            log.info("voices: микрофон не похож на владельца (%.3f) — не учим", raw if raw is not None else 0.0)
            return 0
    if len(clean) > OWNER_LEARN_MAX:
        # равномерно по времени, а не первые N: профиль не должен знать только начало встречи
        picks = np.linspace(0, len(clean) - 1, OWNER_LEARN_MAX).round().astype(int)
        clean = [clean[i] for i in picks]
    source = f"{session_id}:{OWNER_SOURCE_SUFFIX}"
    lib.forget_source(cfg.owner_name, source)
    prof = lib.add(cfg.owner_name, clean, source)
    if prof is None:
        return 0
    log.info("voices: %s ← микрофон, +%d отпечатков (всего %d)", cfg.owner_name, len(clean), prof.count)
    return len(clean)


def owner_mature(cfg: Config, library: VoiceLibrary) -> bool:
    """Профиль владельца набрал достаточно отпечатков, чтобы ему доверять."""
    prof = library.get(cfg.owner_name)
    return prof is not None and prof.count >= cfg.owner_min_samples


ME = "Me"


def _raw_similarity(cfg: Config, library: VoiceLibrary, centroid: list[float]) -> float | None:
    """Косинус центроида с профилем владельца без порога; None — сравнить нельзя (нет профиля, другая размерность)."""
    prof = library.get(cfg.owner_name)
    a, b = unit(centroid), unit(prof.centroid) if prof is not None else None
    if a is None or b is None or len(a) != len(b):
        return None
    return float(np.dot(a, b))


def _owner_similarity(cfg: Config, library: VoiceLibrary, centroid: list[float]) -> float | None:
    """Сходство центроида с профилем владельца, если оно проходит порог библиотеки; иначе None."""
    sim = _raw_similarity(cfg, library, centroid)
    return sim if sim is not None and sim >= library.similarity else None


def relabel_mic(cfg: Config, st: State, library: VoiceLibrary, taken: set[str],
                n_sys_clusters: int) -> dict[int, str]:
    """Расставить метки mic-записям по кластерам голоса; вернуть {кластер: метка}.

    Живые вживую поставленные ``Me`` могли быть неверны: в переговорной на микрофон попадают
    коллеги. Поэтому на финализации перекластеризуем ВСЕ mic-эмбеддинги, а центроид и порог
    ``voice_min_samples`` считаем только по живым (без эха и галлюцинаций). Владелец остаётся
    ``Me``; известный библиотеке голос получает имя, если оно не занято каналом собеседников
    (одно имя — один человек); остальные — ``Speaker N`` после кластеров системного звука,
    чтобы метки двух каналов не пересекались. Слишком малый кластер — ``Me``: по паре реплик
    отпечаток врёт, а ошибиться в пользу владельца безопаснее.
    """
    st.mic_speaker_names = {}
    if len(st.mic_embeddings) >= 2:
        arr = np.asarray(st.mic_embeddings, dtype=np.float64)
        st.mic_cluster_of_emb = [int(c) for c in absorb_small_clusters(recluster(arr, cfg.speaker_similarity), arr)]
    else:
        st.mic_cluster_of_emb = [0] * len(st.mic_embeddings)
    live: dict[int, list[list[float]]] = {}
    for idx, vec in live_mic_embeddings(st):
        if idx < len(st.mic_cluster_of_emb):
            live.setdefault(st.mic_cluster_of_emb[idx], []).append(vec)
    taken = set(taken)
    clusters = sorted(set(st.mic_cluster_of_emb))
    centroids: dict[int, list[float] | None] = {}
    owner_sims: dict[int, float | None] = {}
    hits: dict[int, tuple[str, float] | None] = {}
    for cluster in clusters:
        embs = live.get(cluster, [])
        centroid = centroid_of(embs) if len(embs) >= cfg.voice_min_samples else None
        centroids[cluster] = centroid
        # владельца сверяем напрямую, без зазора: рядом может лежать похожий профиль коллеги, и
        # match() тогда вернёт None — а ошибиться в пользу владельца безопаснее, чем сделать его гостем
        owner_sims[cluster] = _owner_similarity(cfg, library, centroid) if centroid else None
        hits[cluster] = library.match(centroid) if centroid and owner_sims[cluster] is None else None
    owner_clusters = {c for c in clusters if centroids[c] is None or owner_sims[c] is not None
                      or (hits[c] is not None and hits[c][0] == cfg.owner_name)}
    forced: int | None = None
    if clusters and not owner_clusters:
        # микрофон — это владелец: если ни один кластер не дотянул до профиля (простуда, другая
        # гарнитура), самый похожий всё равно остаётся Me, иначе из записи исчезнет хозяин
        def closeness(c: int) -> tuple[float, int]:
            raw = _raw_similarity(cfg, library, centroids[c] or [])
            return (raw if raw is not None else -2.0, len(live.get(c, [])))
        forced = max(clusters, key=closeness)
        log.info("voices: ни один кластер микрофона не дотянул до владельца — самый похожий остаётся %s (%.3f)",
                 ME, max(closeness(forced)[0], -1.0))
    labels: dict[int, str] = {}
    anonymous = 0          # порядковый номер безымянного гостя: Me в нумерацию дыр не вносит
    for cluster in clusters:
        centroid, owner_sim, hit = centroids[cluster], owner_sims[cluster], hits[cluster]
        if centroid is None or cluster == forced:
            labels[cluster] = ME
        elif owner_sim is not None:
            log.info("voices: микрофон, кластер %d → %s (%.3f, напрямую по профилю)", cluster, ME, owner_sim)
            labels[cluster] = ME
        elif hit is not None and hit[0] == cfg.owner_name:
            labels[cluster] = ME
        elif hit is not None and hit[0] not in taken:
            labels[cluster] = hit[0]
            taken.add(hit[0])
        else:
            labels[cluster] = label(n_sys_clusters + anonymous)
            anonymous += 1
    st.mic_speaker_names = {str(c): n for c, n in sorted(labels.items()) if n != ME}
    # короткая реплика без вектора наследует метку предыдущей mic-реплики, первая — Me
    last = ME
    for r in sorted((r for r in st.records if r.channel == "mic"), key=lambda r: r.start):
        if r.emb is not None and 0 <= r.emb < len(st.mic_cluster_of_emb):
            last = labels.get(st.mic_cluster_of_emb[r.emb], ME)
        r.speaker = last
    return labels
