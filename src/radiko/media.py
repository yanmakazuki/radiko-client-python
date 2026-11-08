"""MP4 の箱の上限、ADTS の境界、AAC の復号。

復号そのものは PyAV（同梱の libavcodec）を使う。Rust 版の rusty_aac と同じ
PCM ビット列にはならない。拒否するヘッダーと箱の上限は同じ規則にする。
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass

import av

MAX_SEGMENT_BYTES = 8 * 1024 * 1024
MAX_PACKETS = 4096
MAX_ENTRIES = 4096
MAX_BOXES = 8192
MAX_PCM_BYTES = 4 * 1024 * 1024
_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


class AacError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(str(self))

    def __str__(self) -> str:
        return {
            "cancelled": "AACデコードを取り消しました",
            "large": "AACセグメントのサイズ上限を超えました",
            "metadata": "AACに付加されたID3の形式が不正です",
            "container": "AACコンテナの時刻またはデータ構造が不正です",
            "unsupported-container": "AACコンテナの構成が未対応です",
            "config": "AACの設定情報（ASC）が不正・未対応です",
            "packet": "AACアクセスユニットに余分なデータがあります",
            "header": "ADTSヘッダーが不正です",
            "truncated": "ADTSフレームが途中で切れています",
            "empty": "AAC音声フレームがありません",
            "many": "AACセグメントのパケット数上限を超えました",
            "transport": "CRC付きまたは複数raw blockのADTSは検証対象外です",
            "codec": "AACの構成がデコーダーで未対応です",
            "decode": "AACパケットのデコードに失敗しました",
            "changed": "AACの構成が変わりました。新しいデコーダーで再開してください",
            "pcm": "デコードしたPCMの形式またはサンプル値が不正です",
            "sbr-missing": "HLSはHE-AACを宣言していますが、このサンプルではSBR再構成を確認できません",
            "sbr-rate": "SBRデータを検出しましたが出力がコアレートのままです",
        }[self.code]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AacError) and self.code == other.code


@dataclass
class AacSummary:
    core_sample_rate: int
    core_channels: int
    sample_rate: int
    channels: int
    packets: int
    frames: int
    sbr_detected: bool
    stereo_expansion: bool
    explicit_ps: bool
    media_duration: float
    decode_duration: float
    nonzero_samples: int


@dataclass
class DecodedPcm:
    summary: AacSummary
    samples: list[float]

    def __repr__(self) -> str:
        return f"DecodedPcm(summary={self.summary!r}, samples=[REDACTED])"


@dataclass
class _Frame:
    object_type: int
    sample_rate: int
    channels: int
    payload: bytes


def audio_offset(data: bytes) -> int:
    if data.startswith(b"ID3"):
        if len(data) < 10:
            raise AacError("metadata")
        size = _synchsafe(data[6:10])
        return 10 + size
    return 0


def _synchsafe(raw: bytes) -> int:
    if any(byte & 0x80 for byte in raw):
        raise AacError("metadata")
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def split_adts(data: bytes, cancelled) -> list[_Frame]:
    if cancelled():
        raise AacError("cancelled")
    if len(data) > MAX_SEGMENT_BYTES:
        raise AacError("large")
    offset = audio_offset(data)
    if offset > len(data):
        raise AacError("metadata")
    remaining = data[offset:]
    frames: list[_Frame] = []
    while remaining:
        if cancelled():
            raise AacError("cancelled")
        if len(frames) >= MAX_PACKETS:
            raise AacError("many")
        if len(remaining) < 7:
            raise AacError("truncated")
        header = _parse_adts(remaining)
        if header["frame_length"] <= header["header_len"]:
            raise AacError("header")
        if header["frame_length"] > len(remaining):
            raise AacError("truncated")
        if header["header_len"] != 7 or remaining[6] & 3 != 0:
            raise AacError("transport")
        if header["channels"] == 0 or header["channels"] > 7 or header["object_type"] != 2:
            raise AacError("codec")
        frames.append(
            _Frame(
                header["object_type"],
                header["sample_rate"],
                header["channels"],
                remaining[header["header_len"] : header["frame_length"]],
            )
        )
        remaining = remaining[header["frame_length"] :]
    if not frames:
        raise AacError("empty")
    return frames


def _parse_adts(data: bytes) -> dict:
    if data[0] != 0xFF or data[1] & 0xF0 != 0xF0:
        raise AacError("header")
    if (data[1] >> 1) & 0x3 != 0:
        raise AacError("header")
    protection_absent = data[1] & 1
    profile = (data[2] >> 6) & 0x3
    rate_index = (data[2] >> 2) & 0xF
    if rate_index >= len(_RATES):
        raise AacError("header")
    channels = ((data[2] & 1) << 2) | ((data[3] >> 6) & 0x3)
    frame_length = ((data[3] & 0x3) << 11) | (data[4] << 3) | ((data[5] >> 5) & 0x7)
    header_len = 7 if protection_absent else 9
    return {
        "object_type": profile + 1,
        "sample_rate": _RATES[rate_index],
        "channels": channels,
        "frame_length": frame_length,
        "header_len": header_len,
    }


class AdtsDecoder:
    """セグメントをまたいで形式を覚え、失敗したら状態を捨てる。"""

    def __init__(self) -> None:
        self.format: tuple[int, int, int] | None = None

    def decode_segment_pcm(
        self, data: bytes, declared: str | None = None, cancelled=None
    ) -> DecodedPcm:
        cancelled = cancelled or (lambda: False)
        try:
            frames = split_adts(data, cancelled)
            if cancelled():
                raise AacError("cancelled")
            current = (frames[0].object_type, frames[0].sample_rate, frames[0].channels)
            if any(
                (frame.object_type, frame.sample_rate, frame.channels) != current
                for frame in frames
            ):
                raise AacError("changed")
            if self.format is not None and self.format != current:
                raise AacError("changed")
            started = time.perf_counter()
            samples, rate, channels = _decode_adts(data)
            if cancelled():
                raise AacError("cancelled")
            if len(samples) * 4 > MAX_PCM_BYTES:
                raise AacError("large")
            summary = AacSummary(
                core_sample_rate=current[1],
                core_channels=current[2],
                sample_rate=rate,
                channels=channels,
                packets=len(frames),
                frames=len(samples) // max(channels, 1),
                sbr_detected=rate > current[1],
                stereo_expansion=channels > current[2],
                explicit_ps=False,
                media_duration=(len(samples) // max(channels, 1)) / rate,
                decode_duration=time.perf_counter() - started,
                nonzero_samples=sum(sample != 0.0 for sample in samples),
            )
            if summary.sbr_detected and summary.sample_rate <= summary.core_sample_rate:
                raise AacError("sbr-rate")
            if declared and any(
                part.strip() in {"mp4a.40.5", "mp4a.40.29"} for part in declared.split(",")
            ):
                if not summary.sbr_detected:
                    raise AacError("sbr-missing")
            self.format = current
            return DecodedPcm(summary, samples)
        except Exception:
            self.format = None
            raise


def _decode_adts(data: bytes) -> tuple[list[float], int, int]:
    try:
        container = av.open(io.BytesIO(data), format="aac")
    except av.FFmpegError as error:
        raise AacError("decode") from error
    samples: list[float] = []
    rate = 0
    channels = 0
    try:
        for frame in container.decode(audio=0):
            rate = frame.sample_rate
            channels = len(frame.layout.channels)
            plane = frame.to_ndarray()
            if plane.ndim == 1:
                samples.extend(float(value) for value in plane)
                channels = 1
            else:
                channels = plane.shape[0]
                interleaved = plane.T.reshape(-1)
                samples.extend(float(value) for value in interleaved)
    except av.FFmpegError as error:
        raise AacError("decode") from error
    finally:
        container.close()
    if not samples or rate <= 0 or channels <= 0:
        raise AacError("empty")
    return samples, rate, channels


def validate_mp4(data: bytes) -> None:
    if not _looks_like_box(data):
        if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
            return
        window = data[: min(len(data), 1024 * 1024 + 16)]
        marker = window.find(b"ftyp")
        if marker < 0:
            return
        start = marker - 4
        if start < 0:
            raise AacError("container")
        _walk(data[start:], 0, [0], [0])
        return
    _walk(data, 0, [0], [0])


def _looks_like_box(data: bytes) -> bool:
    return data[4:8] in {b"ftyp", b"styp", b"moov", b"moof", b"mdat", b"free", b"skip", b"wide"}


def _word(data: bytes, offset: int) -> int:
    chunk = data[offset : offset + 4]
    if len(chunk) != 4:
        raise AacError("container")
    return int.from_bytes(chunk, "big")


def _walk(data: bytes, depth: int, boxes: list[int], samples: list[int]) -> None:
    if depth > 16:
        raise AacError("container")
    view = data
    while view:
        boxes[0] += 1
        if boxes[0] > MAX_BOXES:
            raise AacError("many")
        size = _word(view, 0)
        kind = view[4:8]
        if len(kind) != 4:
            raise AacError("container")
        if size == 0:
            size = len(view)
            header = 8
        elif size == 1:
            if len(view) < 16:
                raise AacError("container")
            size = int.from_bytes(view[8:16], "big")
            header = 16
        else:
            header = 8
        if size < header or size > len(view):
            raise AacError("container")
        payload = view[header:size]
        if kind in {
            b"moov",
            b"trak",
            b"mdia",
            b"minf",
            b"stbl",
            b"mvex",
            b"moof",
            b"traf",
            b"edts",
        }:
            _walk(payload, depth + 1, boxes, samples)
        elif kind in {b"trun", b"stsz", b"stz2"}:
            offset = 4 if kind == b"trun" else 8
            count = _word(payload, offset)
            samples[0] += count
            if samples[0] > MAX_ENTRIES:
                raise AacError("many")
        elif kind in {b"stts", b"ctts", b"stsc", b"stco", b"co64", b"stss", b"elst"}:
            count = _word(payload, 4)
            if count > MAX_ENTRIES:
                raise AacError("many")
            if kind == b"stts":
                expanded = 0
                for index in range(count):
                    expanded += _word(payload, 8 + index * 8)
                    if expanded > MAX_ENTRIES:
                        raise AacError("many")
        view = view[size:]


def encode_adts_sine(packets: int = 2, rate: int = 44100) -> bytes:
    """試験用の AAC-LC ADTS。実配信のバイト列ではない。"""
    import math

    import numpy as np

    output = io.BytesIO()
    container = av.open(output, mode="w", format="adts")
    stream = container.add_stream("aac", rate=rate)
    stream.layout = "mono"
    for index in range(packets):
        samples = np.zeros((1, 1024), dtype="float32")
        for offset in range(1024):
            position = index * 1024 + offset
            samples[0, offset] = 0.2 * math.sin(2 * math.pi * 440 * position / rate)
        frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="mono")
        frame.sample_rate = rate
        frame.pts = index * 1024
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return output.getvalue()
