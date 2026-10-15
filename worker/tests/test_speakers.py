import numpy as np
from transcriber.speakers import OnlineClusterer, absorb_small_clusters, label, recluster


def v(*xs):
    return np.array(xs, dtype=np.float32)


def test_online_assigns_same_and_new():
    c = OnlineClusterer(similarity=0.5)
    assert c.assign(v(1, 0, 0)) == 0
    assert c.assign(v(0.9, 0.1, 0)) == 0        # close → same speaker
    assert c.assign(v(0, 1, 0)) == 1            # orthogonal → new
    assert c.assign(v(0, 0.95, 0.05)) == 1
    assert c.n_speakers == 2
    assert label(1) == "Speaker 2"


def test_online_restore_from_history():
    c = OnlineClusterer.from_history(similarity=0.5, embeddings=[[1, 0], [0, 1], [1, 0]], labels=[0, 1, 0])
    assert c.n_speakers == 2
    assert c.assign(v(0.99, 0.01)) == 0


def test_recluster_relabels_by_first_appearance():
    embs = np.array([[0, 1], [1, 0], [0, 1], [1, 0], [0.9, 0.1]], dtype=np.float32)
    assert recluster(embs, similarity=0.5) == [0, 1, 0, 1, 1]


def test_recluster_trivial_sizes():
    assert recluster(np.zeros((0, 4), dtype=np.float32), 0.5) == []
    assert recluster(np.array([[1, 0]], dtype=np.float32), 0.5) == [0]


def test_recluster_masks_nonfinite_and_zero_rows():
    embs = np.array(
        [[1, 0], [float("nan"), float("nan")], [1, 0], [0, 1], [0, 0]],
        dtype=np.float32,
    )
    assert recluster(embs, similarity=0.5) == [0, 0, 0, 1, 1]


def test_online_assign_handles_nonfinite_embedding():
    c = OnlineClusterer(similarity=0.5)
    assert c.assign(v(1, 0)) == 0
    assert c.assign(v(float("nan"), float("nan"))) == 0
    assert c.n_speakers == 1
    assert c.assign(v(1, 0)) == 0


def test_online_restore_from_history_contiguous_labels():
    c = OnlineClusterer.from_history(similarity=0.5, embeddings=[[1, 0], [0, 1], [1, 0]], labels=[0, 1, 0])
    assert c.n_speakers == 2


def test_absorb_merges_singleton_into_nearest_cluster():
    # два настоящих спикера по два эмбеддинга + одиночка, близкая к первому
    embs = [[1, 0, 0], [0.95, 0.05, 0], [0, 1, 0], [0, 0.95, 0.05], [0.9, 0.2, 0.1]]
    labels = [0, 0, 1, 1, 2]
    assert absorb_small_clusters(labels, embs, min_size=2) == [0, 0, 1, 1, 0]


def test_absorb_keeps_singleton_when_nothing_is_close():
    embs = [[1, 0, 0], [0.95, 0.05, 0], [0, 1, 0], [0, 0.95, 0.05], [0, 0, 1]]
    labels = [0, 0, 1, 1, 2]
    assert absorb_small_clusters(labels, embs, min_size=2, min_similarity=0.1) == [0, 0, 1, 1, 2]


def test_absorb_relabels_by_first_appearance():
    # одиночка идёт первой: после слияния метки должны пересчитаться по первому появлению
    embs = [[0.9, 0.2, 0.1], [0, 1, 0], [0, 0.95, 0.05], [1, 0, 0], [0.95, 0.05, 0]]
    labels = [2, 1, 1, 0, 0]
    assert absorb_small_clusters(labels, embs, min_size=2) == [0, 1, 1, 0, 0]


def test_absorb_noop_when_every_cluster_is_small():
    embs = [[1, 0], [0, 1]]
    assert absorb_small_clusters([0, 1], embs, min_size=2) == [0, 1]


def test_absorb_survives_zero_and_nonfinite_rows():
    embs = [[1, 0], [0.9, 0.1], [0.0, 0.0], [float("nan"), 1.0]]
    got = absorb_small_clusters([0, 0, 1, 2], embs, min_size=2)
    assert got[:2] == [0, 0] and len(got) == 4


# ---- разрез склеенного кластера по именам Толка (тикет 04) ------------------------


def _vec(f):
    from tests.conftest import one_hot
    return one_hot(f)


def test_split_by_names_divides_a_cluster_shared_by_two_names():
    from transcriber.speakers import split_by_names
    # один кластер: Анна (тон 100) ×4 и Иван (тон 200) ×4, плюс две безымянные реплики с тоном Ивана
    embs = [_vec(100)] * 4 + [_vec(200)] * 4 + [_vec(200)] * 2
    names = ["Анна Петрова"] * 4 + ["Иван Сидоров"] * 4 + [None, None]
    labels, splits = split_by_names([0] * 10, embs, names, min_share=0.3)
    assert splits == 1
    assert len(set(labels[:4])) == 1 and len(set(labels[4:])) == 1 and labels[0] != labels[4]
    assert labels[8] == labels[4] and labels[9] == labels[4]      # остаток — к ближайшему центроиду


def test_split_by_names_leaves_a_cluster_with_one_dominant_name_alone():
    from transcriber.speakers import split_by_names
    embs = [_vec(100)] * 8 + [_vec(200)] * 2
    names = ["Анна Петрова"] * 8 + ["Иван Сидоров"] * 2         # Иван — 0.2 < 0.3: перебивка, не склейка
    labels, splits = split_by_names([0] * 10, embs, names, min_share=0.3)
    assert splits == 0 and set(labels) == {0}


def test_split_by_names_handles_three_names_and_keeps_other_clusters():
    from transcriber.speakers import split_by_names
    embs = [_vec(100)] * 3 + [_vec(200)] * 3 + [_vec(300)] * 3 + [_vec(400)] * 2
    names = ["А"] * 3 + ["Б"] * 3 + ["В"] * 3 + [None, None]
    labels, splits = split_by_names([0] * 9 + [1] * 2, embs, names, min_share=0.3)
    assert splits == 1 and len(set(labels)) == 4
    assert labels[9] == labels[10] and labels[9] not in set(labels[:9])
    assert sorted(set(labels)) == [0, 1, 2, 3]                    # метки перенумерованы подряд


def test_split_by_names_is_off_at_share_zero_and_ignores_unnamed_clusters():
    from transcriber.speakers import split_by_names
    embs = [_vec(100)] * 3 + [_vec(200)] * 3
    names = ["А"] * 3 + ["Б"] * 3
    assert split_by_names([0] * 6, embs, names, min_share=0.0) == ([0] * 6, 0)
    assert split_by_names([0] * 6, embs, [None] * 6, min_share=0.3) == ([0] * 6, 0)


def test_split_by_names_needs_a_minimum_count_per_half():
    from transcriber.speakers import split_by_names
    # три реплики: две Анны и одна «чужая» плитка на перебивке — доля 0.33, но это не склейка
    embs = [_vec(100)] * 3
    names = ["Анна Петрова", "Анна Петрова", "Иван Сидоров"]
    assert split_by_names([0] * 3, embs, names, min_share=0.3) == ([0] * 3, 0)
    labels, splits = split_by_names([0] * 3, embs, names, min_share=0.3, min_count=1)
    assert splits == 1 and len(set(labels)) == 2
