import numpy as np
from transcriber.vad import CHUNK, Segmenter
from tests.conftest import SR, fake_prob, silence, tone


def seg(**kw):
    args = dict(sample_rate=SR, threshold=0.5, min_silence_ms=700, speech_pad_ms=100, max_utterance_s=30.0)
    args.update(kw)
    return Segmenter(fake_prob, **args)


def test_two_utterances_split_by_silence():
    s = seg()
    audio = np.concatenate([silence(0.5), tone(1.0), silence(1.0), tone(2.0, 880), silence(1.0)])
    utts = s.feed(audio)
    assert len(utts) == 2
    a, b = utts
    assert abs(a.start / SR - 0.4) < 0.06          # 0.5 minus 100 ms pad, chunk granularity 32 ms
    assert abs(a.end / SR - 1.6) < 0.05            # speech ends at 1.5, plus pad
    assert abs(b.start / SR - 2.4) < 0.06
    assert abs(b.end / SR - 4.6) < 0.05
    assert len(a.audio) == a.end - a.start
    assert np.abs(a.audio).max() > 0.2


def test_open_speech_not_emitted_until_silence():
    s = seg()
    assert s.feed(np.concatenate([silence(0.2), tone(1.0)])) == []
    assert s.open_start is not None
    assert s.boundary() == s.open_start
    utts = s.feed(silence(1.0))
    assert len(utts) == 1
    assert s.open_start is None
    assert s.boundary() == s.pos


def test_long_speech_is_capped():
    s = seg(max_utterance_s=2.0)
    utts = s.feed(np.concatenate([tone(5.0), silence(1.0)]))
    assert [round((u.end - u.start) / SR, 1) for u in utts] == [2.0, 2.0, 1.1]


def test_remainder_and_start_sample():
    s = Segmenter(fake_prob, start_sample=16000)
    s.feed(tone(0.02))  # 320 samples: less than one chunk, kept
    assert s.pos == 16000
    s.feed(tone(0.02))  # now 640: one chunk consumed
    assert s.pos == 16512


def test_short_blip_below_min_silence_stays_one_utterance():
    s = seg()
    utts = s.feed(np.concatenate([tone(1.0), silence(0.3), tone(1.0), silence(1.0)]))
    assert len(utts) == 1


def test_flush_closes_open_utterance():
    s = seg()
    assert s.feed(tone(2.0)) == []          # речь не кончилась — ничего не отдано
    assert s.open_start is not None
    utts = s.flush()
    assert len(utts) == 1
    u = utts[0]
    assert u.end == s.pos                   # закрыто ровно на текущей позиции
    assert len(u.audio) == u.samples
    assert s.open_start is None
    assert s.flush() == []                  # второй flush пуст


def test_flush_without_open_utterance_is_empty():
    s = seg()
    s.feed(np.concatenate([tone(0.5), silence(1.0)]))
    assert s.flush() == []


def test_resume_from_boundary_reproduces_utterances():
    stream = np.concatenate([silence(0.5), tone(1.0, 440), silence(1.0), tone(1.5, 880), silence(1.0)])
    full = seg().feed(stream)
    assert len(full) == 2
    cut = int(3.0 * SR)                     # середина второй реплики
    s = seg()
    s.feed(stream[:cut])
    b = s.boundary()
    assert s.open_start is not None and b < cut
    r = seg(start_sample=b, preroll=stream[max(0, b - s.pad):b])
    rest = r.feed(stream[b:])
    assert len(rest) == 1
    assert abs(rest[0].start - full[1].start) <= CHUNK
    assert abs(rest[0].end - full[1].end) <= CHUNK


def test_preroll_restores_pad_lost_on_resume():
    stream = np.concatenate([silence(1.0), tone(1.0), silence(1.0)])
    full = seg().feed(stream)
    assert len(full) == 1
    cut = 30 * CHUNK                        # 15360 — тишина прямо перед речью на 16000
    s = seg()
    assert s.feed(stream[:cut]) == []
    b = s.boundary()
    assert b == cut
    r = seg(start_sample=b, preroll=stream[b - s.pad:b])
    utts = r.feed(stream[b:])
    assert len(utts) == 1
    assert utts[0].start == full[0].start    # pad сохранён точно
    bare = seg(start_sample=b)               # без pre-roll начало уезжает вперёд
    assert bare.feed(stream[b:])[0].start > full[0].start


def test_zero_pad_keeps_tail_empty():
    s = seg(speech_pad_ms=0)
    s.feed(silence(3.0))
    assert len(s.tail) == 0
    assert len(s.feed(np.concatenate([tone(1.0), silence(1.0)]))) == 1
    assert len(s.tail) == 0
