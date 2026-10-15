import numpy as np
from transcriber.pcm import PcmReader, read_range
from tests.conftest import to_pcm_bytes, tone


def test_reads_growing_file_from_offset(tmp_path):
    p = tmp_path / "mic.pcm"
    a = tone(1.0)
    p.write_bytes(to_pcm_bytes(a[:8000]))
    r = PcmReader(p, offset_samples=4000)
    assert r.available() == 4000
    got = r.read_all()
    assert len(got) == 4000
    assert np.allclose(got, a[4000:8000], atol=1e-3)
    assert r.offset == 8000
    assert r.read_all().size == 0
    with open(p, "ab") as f:
        f.write(to_pcm_bytes(a[8000:]))
    assert r.available() == 8000
    assert len(r.read_all()) == 8000
    assert r.offset == 16000


def test_missing_file_is_empty(tmp_path):
    r = PcmReader(tmp_path / "nope.pcm")
    assert r.available() == 0
    assert r.read_all().size == 0


def test_read_all_respects_max_samples(tmp_path):
    p = tmp_path / "mic.pcm"
    p.write_bytes(to_pcm_bytes(tone(1.0)))
    r = PcmReader(p)
    assert len(r.read_all(max_samples=4000)) == 4000
    assert r.offset == 4000
    assert len(r.read_all()) == 12000


def test_read_range_window_and_short_file(tmp_path):
    p = tmp_path / "mic.pcm"
    a = tone(1.0)
    p.write_bytes(to_pcm_bytes(a))
    got = read_range(p, 1000, 2000)
    assert len(got) == 1000
    assert np.allclose(got, a[1000:2000], atol=1e-3)
    assert len(read_range(p, 15500, 17000)) == 500      # хвост короче запроса
    assert read_range(p, 0, 0).size == 0
    assert len(read_range(p, -100, 100)) == 100         # старт зажимается нулём
    assert read_range(tmp_path / "nope.pcm", 0, 100).size == 0


def test_odd_trailing_byte_is_ignored(tmp_path):
    p = tmp_path / "mic.pcm"
    p.write_bytes(to_pcm_bytes(tone(0.1)) + b"\x01")
    assert len(read_range(p, 1500, 1700)) == 100
