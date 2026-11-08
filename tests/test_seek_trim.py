"""セグメント途中の切り詰め。ナノ秒からフレームへ切り上げる。"""

from datetime import datetime, timedelta, timezone

import pytest

from radiko.stream import ProbeError
from radiko.timing import trim_pcm

START = datetime(2026, 10, 15, 12, 0, tzinfo=timezone(timedelta(hours=9)))


def test_trim_discards_a_finished_segment_and_keeps_the_interior():
    rate = 48_000
    channels = 2
    samples = [0.5] * (rate * channels)
    later = START + timedelta(seconds=1.25)
    assert trim_pcm(samples, channels, rate, START, later) is None
    kept, frames = trim_pcm(samples, channels, rate, START + timedelta(seconds=1), later)
    assert frames == rate - rate // 4
    assert len(kept) == frames * channels


def test_trim_rounds_a_partial_frame_up_and_rejects_a_missing_clock():
    samples = [0.25] * 8
    kept, frames = trim_pcm(samples, 2, 48_000, START, START + timedelta(microseconds=1))
    assert frames == 3
    assert len(kept) == 6
    with pytest.raises(ProbeError) as raised:
        trim_pcm(samples, 2, 48_000, None, START)
    assert raised.value.code == "playlist"
