from __future__ import annotations

import re

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage


def label(idx: int) -> str:
    return f"Speaker {idx + 1}"


def speaker_index(name: str) -> int | None:
    """Обратное к ``label``: «Speaker 3» → 3; любое другое имя (человек, «Me») → None."""
    m = re.fullmatch(r"Speaker\s+(\d+)", name or "")
    return int(m.group(1)) if m else None


def _unit(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = float(np.linalg.norm(x))
    return x / n if n > 0 else x


class OnlineClusterer:
    """Assigns each embedding to the nearest centroid (cosine) or opens a new speaker."""

    def __init__(self, similarity: float):
        self.similarity = similarity
        self.sums: list[np.ndarray] = []
        self.counts: list[int] = []
        self.last: int = 0

    @classmethod
    def from_history(cls, similarity: float, embeddings: list[list[float]], labels: list[int]) -> "OnlineClusterer":
        c = cls(similarity)
        agg: dict[int, tuple[np.ndarray, int]] = {}
        for e, l in zip(embeddings, labels):
            u = _unit(np.asarray(e))
            if l in agg:
                s, n = agg[l]
                agg[l] = (s + u, n + 1)
            else:
                agg[l] = (u, 1)
        if agg:
            ordered = sorted(agg)
            assert ordered == list(range(len(ordered))), "from_history: labels must be contiguous 0..k-1"
            for l in ordered:
                s, n = agg[l]
                c.sums.append(s)
                c.counts.append(n)
        return c

    @property
    def n_speakers(self) -> int:
        return len(self.sums)

    def assign(self, emb: np.ndarray) -> int:
        raw = np.asarray(emb, dtype=np.float32)
        if not np.all(np.isfinite(raw)) or float(np.linalg.norm(raw)) == 0.0:
            return self.last
        e = _unit(raw)
        if self.sums:
            sims = [float(np.dot(e, _unit(s))) for s in self.sums]
            sims = [s if np.isfinite(s) else -1.0 for s in sims]
            best = int(np.argmax(sims))
            if sims[best] >= self.similarity:
                self.sums[best] = self.sums[best] + e
                self.counts[best] += 1
                self.last = best
                return best
        self.sums.append(e.copy())
        self.counts.append(1)
        self.last = len(self.sums) - 1
        return self.last


def _relabel(labels: list[int]) -> list[int]:
    """Renumber cluster ids 0..k-1 in order of first appearance."""
    order: dict[int, int] = {}
    return [order.setdefault(int(l), len(order)) for l in labels]


def absorb_small_clusters(labels: list[int], embeddings, min_size: int = 2,
                          min_similarity: float = 0.1) -> list[int]:
    """Merge clusters below ``min_size`` into the nearest normal-sized one by centroid cosine.

    Single-utterance clusters are almost always whisper junk or a badly cut segment, not a person:
    on the first real meeting they were a hallucinated English line and a stretch of «Аааа...».
    A tiny cluster is kept as its own speaker when even the nearest centroid is below
    ``min_similarity``, and when there is no cluster of ``min_size`` or more to merge into.
    Result is relabelled by first appearance.
    """
    labels = [int(l) for l in labels]
    arr = np.asarray(embeddings, dtype=np.float64)
    if not labels or len(arr) != len(labels):
        return _relabel(labels)
    counts: dict[int, int] = {}
    for l in labels:
        counts[l] = counts.get(l, 0) + 1
    big = [l for l in counts if counts[l] >= min_size]
    if not big:
        return _relabel(labels)
    centroids = {l: _unit(np.sum([_unit(arr[i]) for i, x in enumerate(labels) if x == l], axis=0))
                 for l in counts}
    mapping: dict[int, int] = {}
    for l in counts:
        if counts[l] >= min_size:
            mapping[l] = l
            continue
        sims = [(float(np.dot(centroids[l], centroids[b])), b) for b in big]
        sims = [(s if np.isfinite(s) else -1.0, b) for s, b in sims]
        best_sim, best = max(sims)
        mapping[l] = best if best_sim >= min_similarity else l
    return _relabel([mapping[l] for l in labels])


def recluster(embeddings: np.ndarray, similarity: float) -> list[int]:
    """Average-linkage agglomerative clustering on cosine distance; labels ordered by first appearance.

    Rows that are non-finite or have zero norm are masked out of the clustering (scipy's
    ``linkage`` raises on a non-finite condensed distance matrix) and given the label of the
    previous valid row in original order, or 0 if there is none before them.
    """
    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [0]
    arr = np.asarray(embeddings, dtype=np.float64)
    finite = np.isfinite(arr).all(axis=1)
    norms = np.linalg.norm(np.where(finite[:, None], arr, 0.0), axis=1)
    valid = finite & (norms > 0)
    valid_idx = np.nonzero(valid)[0]

    if len(valid_idx) == 0:
        return [0] * n
    if len(valid_idx) == 1:
        valid_labels = [0]
    else:
        x = np.stack([_unit(embeddings[i]) for i in valid_idx])
        z = linkage(x, method="average", metric="cosine")
        raw = fcluster(z, t=1.0 - similarity, criterion="distance")
        valid_labels = _relabel([int(c) for c in raw])

    labels = [0] * n
    last = 0
    vi = 0
    for i in range(n):
        if vi < len(valid_idx) and valid_idx[vi] == i:
            last = valid_labels[vi]
            vi += 1
        labels[i] = last
    return labels


class Embedder:
    """pyannote speaker embedding; returns a 1-D float32 vector."""

    def __init__(self, model_name: str, sample_rate: int = 16000):
        import torch
        from pyannote.audio import Inference, Model

        self.torch = torch
        self.sr = sample_rate
        model = Model.from_pretrained(model_name)
        self.inference = Inference(model, window="whole")

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        wave = self.torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None]
        return np.asarray(self.inference({"waveform": wave, "sample_rate": self.sr}), dtype=np.float32).reshape(-1)


SPLIT_MIN_COUNT = 3   # половине нужно столько названных реплик: одна ошибка плитки на перебивке — не склейка


def split_by_names(labels: list[int], embeddings, names: list[str | None],
                   min_share: float = 0.3, min_count: int = SPLIT_MIN_COUNT) -> tuple[list[int], int]:
    """Разрезать кластеры, которые плитки Толка делят между двумя людьми (тикет 04).

    ``labels``, ``embeddings`` и ``names`` выровнены по индексу эмбеддинга; ``names[i]`` — имя
    плитки, накрывшей реплику, или None. Кластер, где у двух и более имён доля названных реплик
    не меньше ``min_share`` каждое, — склейка: голоса похожи, но интерфейс встречи видел разных
    людей (и у каждого из них не меньше ``min_count`` реплик: Толк на перебивках ошибается, одна
    чужая плитка в маленьком кластере — не склейка). Реплики каждого такого имени становятся своим кластером (самое частое имя оставляет
    себе старую метку), остаток — безымянные и редкие имена — уходит к ближайшему из новых
    центроидов по косинусу: он и так лежал в этом кластере, так что одна из половин ему своя.
    ``min_share <= 0`` выключает разрез. Метки перенумерованы подряд; второе число — сколько
    кластеров разрезано.
    """
    labels = [int(l) for l in labels]
    if min_share <= 0 or not labels or len(labels) != len(names):
        return _relabel(labels), 0
    arr = np.asarray(embeddings, dtype=np.float64)
    if len(arr) != len(labels):
        return _relabel(labels), 0
    members: dict[int, list[int]] = {}
    for i, l in enumerate(labels):
        members.setdefault(l, []).append(i)
    next_label = max(labels) + 1
    splits = 0
    out = list(labels)
    for cluster, idx in members.items():
        counts: dict[str, int] = {}
        for i in idx:
            if names[i]:
                counts[names[i]] = counts.get(names[i], 0) + 1
        named = sum(counts.values())
        majors = [n for n, k in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                  if named and k >= min_count and k / named >= min_share]
        if len(majors) < 2:
            continue
        splits += 1
        label_of = {majors[0]: cluster}
        for n in majors[1:]:
            label_of[n] = next_label
            next_label += 1
        centroids = {n: _unit(np.sum([_unit(arr[i]) for i in idx if names[i] == n], axis=0)) for n in majors}
        for i in idx:
            n = names[i]
            if n in label_of:
                out[i] = label_of[n]
                continue
            e = _unit(arr[i])
            sims = [(float(np.dot(e, centroids[m])), m) for m in majors]
            sims = [(s if np.isfinite(s) else -1.0, m) for s, m in sims]
            out[i] = label_of[max(sims)[1]]
    return _relabel(out), splits
