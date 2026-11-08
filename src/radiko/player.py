"""再生ワーカー。古い世代の PCM は混ぜない。一時停止は出力済みの位置を保つ。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from radiko.hls import HlsCursor, MediaPlaylist, parse_media_playlist
from radiko.media import AdtsDecoder, DecodedPcm
from radiko.output import OutputSummary, play_samples

RING_SECONDS = 2


@dataclass
class PlaybackRequest:
    station: str
    mode: str
    cursor: str
    position: float
    generation: int


@dataclass
class Progress:
    state: str
    station: str
    title: str | None
    mode: str
    starts_at: str
    ends_at: str
    position: float | None
    volume: int
    pending: PlaybackRequest | None = None
    output: OutputSummary | None = None
    segments: int = 0


@dataclass
class PlayReport:
    reason: str
    output: OutputSummary | None
    segments: int
    position: float


@dataclass
class Engine:
    """容量1の最新要求。準備が失敗しても、いまの再生を止めない。"""

    volume: int = 25
    generation: int = 0
    current: PlayReport | None = None
    pending: PlaybackRequest | None = None
    paused: bool = False
    position: float = 0.0
    stop: threading.Event = field(default_factory=threading.Event)
    _pause_flag: threading.Event = field(default_factory=threading.Event)
    messages: list[str] = field(default_factory=list)

    def request(self, station: str, mode: str, cursor: str, position: float) -> int:
        self.generation += 1
        self.pending = PlaybackRequest(station, mode, cursor, position, self.generation)
        return self.generation

    def fail_prepare(self, generation: int, message: str) -> str | None:
        if generation != self.generation:
            return None
        self.pending = None
        text = f"{message}（現在の状態を維持）" if self.current else message
        self.messages.append(text)
        return text

    def commit(
        self, generation: int, samples: list[float], rate: int, channels: int, limit: float | None
    ) -> PlayReport | None:
        if generation != self.generation:
            return None
        self.pending = None
        self.stop.clear()
        if self.paused:
            self._pause_flag.set()
        else:
            self._pause_flag.clear()
        if limit is not None:
            max_frames = int(limit * rate)
            samples = samples[: max_frames * channels]
        summary = play_samples(samples, rate, channels, self.stop, self._pause_flag)
        self.position = summary.frames / rate
        reason = "cancelled" if self.stop.is_set() else "limit" if limit is not None else "end"
        if reason == "end":
            reason = "program-end"
        report = PlayReport(reason, summary, 1, self.position)
        self.current = report
        return report

    def pause(self) -> None:
        self.paused = True
        self._pause_flag.set()

    def resume(self) -> None:
        self.paused = False
        self._pause_flag.clear()

    def cancel(self) -> None:
        self.stop.set()
        self.generation += 1
        self.pending = None

    def seek(self, seconds: float, duration: float) -> str | None:
        if seconds < 0 or seconds >= duration:
            return "シーク位置は番組冒頭以上・放送枠末尾未満で指定してください"
        self.position = seconds
        self.generation += 1
        self.stop.set()
        return None


def play_adts_bytes(
    data: bytes, declared: str | None, stop: threading.Event, limit: float | None = None
) -> DecodedPcm:
    decoder = AdtsDecoder()
    if stop.is_set():
        from radiko.media import AacError

        raise AacError("cancelled")
    pcm = decoder.decode_segment_pcm(data, declared, stop.is_set)
    if limit is not None:
        frames = int(limit * pcm.summary.sample_rate)
        pcm.samples = pcm.samples[: frames * pcm.summary.channels]
    return pcm


def play_playlist(
    text: str, fetch_segment, live: bool, stop: threading.Event, limit: float | None = None
) -> PlayReport:
    media = parse_media_playlist(text)
    cursor = HlsCursor.start(media, live)
    decoder = AdtsDecoder()
    samples: list[float] = []
    rate = 48000
    channels = 2
    segments = 0
    played_seconds = 0.0
    while True:
        if stop.is_set():
            break
        segment = cursor.take()
        if segment is None:
            break
        payload = fetch_segment(segment.uri)
        if stop.is_set():
            break
        if cursor.pending_discontinuity:
            decoder = AdtsDecoder()
            cursor.pending_discontinuity = False
        pcm = decoder.decode_segment_pcm(payload, None, stop.is_set)
        rate = pcm.summary.sample_rate
        channels = pcm.summary.channels
        samples.extend(pcm.samples)
        segments += 1
        played_seconds += pcm.summary.media_duration
        if limit is not None and played_seconds >= limit:
            break
        if media.end_list and cursor.next_sequence >= media.media_sequence + len(media.segments):
            break
    summary = play_samples(samples, rate, channels, stop) if samples else None
    reason = (
        "cancelled"
        if stop.is_set()
        else "limit"
        if limit is not None and played_seconds >= (limit or 0)
        else "program-end"
    )
    position = 0.0 if summary is None else summary.frames / rate
    return PlayReport(reason, summary, segments, position)


def reload_interval(previous: MediaPlaylist, new: MediaPlaylist) -> float:
    cursor = HlsCursor.start(previous, live=True)
    return cursor.install(new)
