import numpy as np
from transcriber.blocks import BlockBuilder
from transcriber.vad import Utterance
from tests.conftest import SR, silence, tone


def utt(start_s: float, dur_s: float, freq: float = 440.0) -> Utterance:
    start = int(start_s * SR)
    audio = tone(dur_s, freq)
    return Utterance(start=start, end=start + len(audio), audio=audio)


def builder(**kw) -> BlockBuilder:
    args = dict(sample_rate=SR, min_s=45.0, max_s=90.0, gap_s=1.5, gap_keep_s=0.5, idle_s=4.0)
    args.update(kw)
    return BlockBuilder(**args)


def test_nothing_is_emitted_while_the_block_is_open():
    b = builder()
    assert b.add(utt(1.0, 2.0)) == []
    assert b.idle(now_samples=int(2.0 * SR)) == []      # пауза ещё короткая
    assert b.open_start == int(1.0 * SR)


def test_empty_builder_emits_nothing():
    b = builder()
    assert b.flush() == []
    assert b.idle(now_samples=10 ** 7) == []
    assert b.open_start is None


def test_gap_between_utterances_is_trimmed_and_times_map_back():
    b = builder()
    b.add(utt(1.0, 2.0, 440))
    b.add(utt(6.0, 2.0, 880))          # пауза 3 с — в склейке останется 0.5 с
    blocks = b.flush()
    assert len(blocks) == 1
    blk = blocks[0]
    assert blk.start == int(1.0 * SR) and blk.end == int(8.0 * SR)
    assert abs(len(blk.audio) / SR - (2.0 + 0.5 + 2.0)) < 1e-6
    m = blk.map_to_original
    assert abs(m(0.0) - 1.0) < 1e-6                     # начало блока
    assert abs(m(1.5) - 2.5) < 1e-6                     # середина первой реплики
    assert abs(m(2.2) - 3.0) < 1e-6                     # внутри вырезанной паузы — конец первой
    assert abs(m(2.5 + 0.2) - 6.2) < 1e-6               # 0.2 с внутри второй реплики
    assert abs(m(99.0) - 8.0) < 1e-6                    # за хвостом — конец блока


def test_block_closes_on_min_span_plus_gap():
    b = builder(min_s=3.0, gap_s=1.5)
    b.add(utt(0.0, 2.0))
    assert b.add(utt(2.5, 2.0)) == []                   # 4.5 с накоплено, но пауза 0.5 с мала
    blocks = b.add(utt(6.0, 1.0))                       # пауза 1.5 с — блок закрывается
    assert len(blocks) == 1
    assert blocks[0].start == 0 and blocks[0].end == int(4.5 * SR)
    assert b.open_start == int(6.0 * SR)                # новая реплика открыла следующий блок


def test_block_closes_on_max_span_at_utterance_boundary():
    b = builder(min_s=45.0, max_s=5.0)
    b.add(utt(0.0, 3.0))
    blocks = b.add(utt(3.2, 3.0))
    assert len(blocks) == 1
    assert blocks[0].end == int(6.2 * SR)
    assert b.open_start is None                         # закрылись ровно на границе реплики


def test_block_closes_on_idle():
    b = builder(idle_s=4.0)
    b.add(utt(0.0, 2.0))
    assert b.idle(now_samples=int(5.9 * SR)) == []
    blocks = b.idle(now_samples=int(6.0 * SR))
    assert len(blocks) == 1 and blocks[0].end == int(2.0 * SR)
    assert b.idle(now_samples=int(60.0 * SR)) == []     # второй раз пусто


def test_flush_closes_and_resets():
    b = builder()
    b.add(utt(0.0, 1.0))
    assert len(b.flush()) == 1
    assert b.flush() == [] and b.open_start is None


def test_audio_of_the_block_is_the_utterances_glued_with_silence():
    b = builder(gap_keep_s=0.5)
    b.add(Utterance(start=0, end=SR, audio=tone(1.0, 440)))
    b.add(Utterance(start=int(4.0 * SR), end=int(5.0 * SR), audio=tone(1.0, 880)))
    blk = b.flush()[0]
    expected = np.concatenate([tone(1.0, 440), silence(0.5), tone(1.0, 880)])
    assert len(blk.audio) == len(expected)
    assert float(np.abs(blk.audio - expected).max()) < 1e-6
