"""断片の時刻照合と、再生開始後の再接続待ち。時計は差し替えられる。"""

from __future__ import annotations

from datetime import datetime

from radiko.hls import segment_wall as _segment_wall
from radiko.media import AacError
from radiko.stream import ProbeError

BACKOFF_START = 1.0
BACKOFF_MAX = 10.0
RECONNECT_LIMIT = 300.0


def accept_fragment(
    extinf: float,
    pcm_seconds: float,
    decode_time: int,
    timescale: int,
    wall: datetime | None,
    stamp: tuple[int, datetime] | None,
) -> tuple[int, datetime] | None:
    """PCM 長と EXTINF、tfdt の差と PROGRAM-DATE-TIME の差は 2ms 以内。"""
    if abs(_microseconds(pcm_seconds) - _microseconds(extinf)) > 2000:
        raise AacError("container")
    if wall is None:
        return stamp
    if stamp is not None:
        ticks = decode_time - stamp[0]
        if ticks < 0 or timescale <= 0:
            raise AacError("container")
        media = ticks / timescale
        delta = (wall - stamp[1]).total_seconds()
        if delta < 0 or abs(_microseconds(media) - _microseconds(delta)) > 2000:
            raise AacError("container")
    return (decode_time, wall)


def segment_wall(media, sequence: int) -> datetime | None:
    """プレイリスト先頭から EXTINF を足して、その通し番号の番組時刻を求める。"""
    return _segment_wall(media, sequence)


def trim_pcm(
    samples: list[float],
    channels: int,
    rate: int,
    start: datetime | None,
    target: datetime | None,
) -> tuple[list[float], int] | None:
    """番組時刻から目標までを切り捨てる。セグメント全体より先なら None。

    捨てるフレーム数はナノ秒×サンプルレートを切り上げる。Rust の `trim_to` と同じ式。
    """
    if start is None or target is None or channels <= 0 or rate <= 0:
        raise ProbeError("playlist")
    delta = (target - start).total_seconds()
    if delta < 0:
        raise ProbeError("playlist")
    nanos = int(round(delta * 1_000_000_000))
    skip = (nanos * rate + 1_000_000_000 - 1) // 1_000_000_000
    frames = len(samples) // channels
    if skip >= frames:
        return None
    kept = samples[skip * channels :]
    return kept, frames - skip


def _microseconds(value: float) -> int:
    return int(round(value * 1_000_000))


class ReconnectBudget:
    """再生が一度始まったあとの通信失敗だけを、1 秒から倍々で最大 5 分待つ。"""

    def __init__(self, cancelled, sleep=None):
        self.cancelled = cancelled
        self.sleep = sleep or __import__("time").sleep
        self.waited = 0.0
        self.backoff = BACKOFF_START
        self.steps: list[float] = []

    def can_retry(self, error: ProbeError, played: bool) -> bool:
        if not played:
            return False
        current = error.inner if error.code == "at" and error.inner is not None else error
        if current.code == "network":
            return True
        return current.code == "http" and (current.status == 408 or (current.status or 0) >= 500)

    def wait(self) -> None:
        if self.cancelled():
            raise ProbeError("cancelled")
        if self.waited >= RECONNECT_LIMIT or self.backoff == 0:
            raise ProbeError("reconnect")
        step = min(self.backoff, BACKOFF_MAX, RECONNECT_LIMIT - self.waited)
        if step <= 0:
            raise ProbeError("reconnect")
        left = step
        while left > 0:
            if self.cancelled():
                raise ProbeError("cancelled")
            slice_ = min(0.01, left)
            self.sleep(slice_)
            left -= slice_
        self.waited += step
        self.steps.append(step)
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)

    def reset(self) -> None:
        """セグメントを返せたら待ちを戻す。次の失敗は再び 1 秒からである。"""
        self.waited = 0.0
        self.backoff = BACKOFF_START
