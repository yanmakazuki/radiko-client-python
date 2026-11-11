"""実 ADTS ペイロードを箱に詰めて、連続フラグメントと壊れた箱を見る。"""

from radiko.fixtures import encode_adts_sine, synthetic_fragment, synthetic_init
from radiko.fragmented import FragmentedAacDecoder
from radiko.media import AacError, split_adts

ASC = bytes([0x12, 0x08])


def _raw_packets(count: int) -> list[bytes]:
    data = encode_adts_sine(packets=count)
    return [frame.payload for frame in split_adts(data, lambda: False)]


def test_two_fragments_keep_timeline_and_a_gap_discards_state():
    packets = _raw_packets(3)
    init = synthetic_init(ASC, 44100, 1024, 0)
    decoder = FragmentedAacDecoder(init)
    first = synthetic_fragment(packets, 0, 1)
    second = synthetic_fragment(packets, 1024 * len(packets), 2)
    opened = decoder.decode_pcm(first)
    followed = decoder.decode_pcm(second)
    assert opened.decode_time == 0
    assert followed.decode_time == 1024 * len(packets)
    assert opened.frames == 1024 * len(packets)
    assert followed.frames == opened.frames
    assert opened.sample_rate == 44100
    broken = synthetic_fragment(packets, followed.decode_time + followed.duration + 1, 3)
    try:
        decoder.decode_pcm(broken)
    except AacError as error:
        assert error.code == "container"
    else:
        raise AssertionError("a tfdt gap must fail")
    assert decoder.next_time is None
    assert decoder.next_sequence is None
    restarted = decoder.decode_pcm(first)
    assert restarted.decode_time == 0


def test_cancel_between_packets_discards_partial_pcm():
    packets = _raw_packets(4)
    decoder = FragmentedAacDecoder(synthetic_init(ASC, 44100, 1024, 0))
    seen = {"n": 0}

    def cancelled():
        seen["n"] += 1
        return seen["n"] > 6

    try:
        decoder.decode_pcm(synthetic_fragment(packets, 1000, 1), cancelled)
    except AacError as error:
        assert error.code == "cancelled"
    else:
        raise AssertionError("cancel must stop the fragment")
    assert decoder.next_time is None
