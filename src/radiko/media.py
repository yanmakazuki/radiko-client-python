"""MP4 の箱の上限、ADTS の境界、AAC の復号。

復号そのものは PyAV（同梱の libavcodec）を使う。Rust 版の rusty_aac と同じ
PCM ビット列にはならない。拒否するヘッダーと箱の上限は同じ規則にする。
"""

from __future__ import annotations

import io
import struct
import time
from dataclasses import dataclass

import av
import numpy as np

MAX_SEGMENT_BYTES = 8 * 1024 * 1024
MAX_PACKETS = 4096
MAX_ENTRIES = 4096
MAX_BOXES = 8192
MAX_PCM_BYTES = 4 * 1024 * 1024
_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]
_ADTS_HEADER = struct.Struct(">BBBBBBB")
_WORD = struct.Struct(">I")


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
    samples: np.ndarray

    def __repr__(self) -> str:
        return f"DecodedPcm(summary={self.summary!r}, samples=[REDACTED])"


@dataclass
class _Frame:
    object_type: int
    sample_rate: int
    channels: int
    payload: bytes


def id3_offset(data: bytes) -> int | None:
    """連続する ID3v2 タグを飛ばした音声先頭。不正なタグは None。"""
    offset = 0
    for _ in range(8):
        if data[offset : offset + 3] != b"ID3":
            return offset if offset <= len(data) else None
        tag = data[offset : offset + 10]
        if len(tag) != 10 or not 2 <= tag[3] <= 4 or any(byte & 0x80 for byte in tag[6:10]):
            return None
        size = (tag[6] << 21) | (tag[7] << 14) | (tag[8] << 7) | tag[9]
        footer = 10 if tag[3] == 4 and tag[5] & 0x10 else 0
        offset += 10 + size + footer
        if offset > len(data):
            return None
    return None


def audio_offset(data: bytes) -> int:
    offset = id3_offset(data)
    if offset is None:
        raise AacError("metadata")
    return offset


def split_adts(data: bytes, cancelled) -> list[_Frame]:
    if cancelled():
        raise AacError("cancelled")
    if len(data) > MAX_SEGMENT_BYTES:
        raise AacError("large")
    offset = audio_offset(data)
    if offset > len(data):
        raise AacError("metadata")
    view = memoryview(data)
    frames: list[_Frame] = []
    _walk_adts(view, len(view), offset, cancelled, frames, False)
    return frames


def _adts_header(view, pos: int) -> tuple[int, int, int, int, int]:
    """(object_type, sample_rate, channels, frame_length, header_len) を整数だけで返す。"""
    first, second, third, fourth, fifth, sixth, _ = _ADTS_HEADER.unpack_from(view, pos)
    if first != 0xFF or second & 0xF0 != 0xF0:
        raise AacError("header")
    if (second >> 1) & 0x3 != 0:
        raise AacError("header")
    profile = (third >> 6) & 0x3
    rate_index = (third >> 2) & 0xF
    if rate_index >= len(_RATES):
        raise AacError("header")
    channels = ((third & 1) << 2) | ((fourth >> 6) & 0x3)
    frame_length = ((fourth & 0x3) << 11) | (fifth << 3) | ((sixth >> 5) & 0x7)
    header_len = 7 if second & 1 else 9
    return profile + 1, _RATES[rate_index], channels, frame_length, header_len


def _walk_adts(view, total: int, pos: int, cancelled, frames, strict: bool):
    """ADTS を1回走査する。frames が None のときはペイロードを複製しない。

    戻り値は (パケット数, 先頭パケットの形式)。strict のときだけ形式の混在を
    「changed」として弾く。検証の順序は split_adts の従来動作に合わせる。
    """
    count = 0
    core = None
    changed = False
    while pos < total:
        if cancelled():
            raise AacError("cancelled")
        if count >= MAX_PACKETS:
            raise AacError("many")
        if total - pos < 7:
            raise AacError("truncated")
        object_type, rate, channels, length, header_len = _adts_header(view, pos)
        if length <= header_len:
            raise AacError("header")
        if length > total - pos:
            raise AacError("truncated")
        if header_len != 7 or view[pos + 6] & 3 != 0:
            raise AacError("transport")
        if channels == 0 or channels > 7 or object_type != 2:
            raise AacError("codec")
        current = (object_type, rate, channels)
        if core is None:
            core = current
        elif core != current:
            changed = True
        if frames is not None:
            frames.append(
                _Frame(object_type, rate, channels, bytes(view[pos + header_len : pos + length]))
            )
        count += 1
        pos += length
    if not count:
        raise AacError("empty")
    if changed and strict:
        raise AacError("changed")
    return count, core


def _parse_adts(data) -> dict:
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
            if cancelled():
                raise AacError("cancelled")
            if len(data) > MAX_SEGMENT_BYTES:
                raise AacError("large")
            offset = audio_offset(data)
            if offset > len(data):
                raise AacError("metadata")
            view = memoryview(data)
            packets, current = _walk_adts(view, len(view), offset, cancelled, None, True)
            if cancelled():
                raise AacError("cancelled")
            if self.format is not None and self.format != current:
                raise AacError("changed")
            started = time.perf_counter()
            samples, rate, channels = _decode_adts(view[offset:])
            if cancelled():
                raise AacError("cancelled")
            if len(samples) * 4 > MAX_PCM_BYTES:
                raise AacError("large")
            summary = AacSummary(
                core_sample_rate=current[1],
                core_channels=current[2],
                sample_rate=rate,
                channels=channels,
                packets=packets,
                frames=samples.size // max(channels, 1),
                sbr_detected=rate > current[1],
                stereo_expansion=channels > current[2],
                explicit_ps=False,
                media_duration=(samples.size // max(channels, 1)) / rate,
                decode_duration=time.perf_counter() - started,
                nonzero_samples=int(np.count_nonzero(samples)),
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


def _decode_adts(data: bytes | memoryview) -> tuple[np.ndarray, int, int]:
    try:
        container = av.open(io.BytesIO(data), format="aac")
    except av.FFmpegError as error:
        raise AacError("decode") from error
    chunks: list[np.ndarray] = []
    rate = 0
    channels = 0
    try:
        for frame in container.decode(audio=0):
            rate = frame.sample_rate
            channels = len(frame.layout.channels)
            plane = frame.to_ndarray()
            if plane.ndim == 1:
                interleaved = plane.reshape(-1)
                channels = 1
            else:
                channels = plane.shape[0]
                interleaved = plane.T.reshape(-1)
            chunks.append(np.ascontiguousarray(interleaved, dtype=np.float32))
    except av.FFmpegError as error:
        raise AacError("decode") from error
    finally:
        container.close()
    if not chunks or rate <= 0 or channels <= 0:
        raise AacError("empty")
    samples = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
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
        _walk(memoryview(data)[start:], 0, 0, 0)
        return
    _walk(data, 0, 0, 0)


def _looks_like_box(data: bytes) -> bool:
    return data[4:8] in {b"ftyp", b"styp", b"moov", b"moof", b"mdat", b"free", b"skip", b"wide"}


def _word(data, offset: int) -> int:
    try:
        return _WORD.unpack_from(data, offset)[0]
    except struct.error as error:
        raise AacError("container") from error


def _walk(data, depth: int, boxes: int, samples: int) -> tuple[int, int]:
    if depth > 16:
        raise AacError("container")
    view = memoryview(data)
    total = len(view)
    pos = 0
    while pos < total:
        boxes += 1
        if boxes > MAX_BOXES:
            raise AacError("many")
        size = _word(view, pos)
        kind = bytes(view[pos + 4 : pos + 8])
        if len(kind) != 4:
            raise AacError("container")
        if size == 0:
            size = total - pos
            header = 8
        elif size == 1:
            if total - pos < 16:
                raise AacError("container")
            size = int.from_bytes(view[pos + 8 : pos + 16], "big")
            header = 16
        else:
            header = 8
        if size < header or size > total - pos:
            raise AacError("container")
        payload = pos + header
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
            boxes, samples = _walk(view[payload : pos + size], depth + 1, boxes, samples)
        elif kind in {b"trun", b"stsz", b"stz2"}:
            offset = 4 if kind == b"trun" else 8
            count = _word(view, payload + offset)
            samples += count
            if samples > MAX_ENTRIES:
                raise AacError("many")
        elif kind in {b"stts", b"ctts", b"stsc", b"stco", b"co64", b"stss", b"elst"}:
            count = _word(view, payload + 4)
            if count > MAX_ENTRIES:
                raise AacError("many")
            if kind == b"stts":
                expanded = 0
                for index in range(count):
                    expanded += _word(view, payload + 8 + index * 8)
                    if expanded > MAX_ENTRIES:
                        raise AacError("many")
        pos += size
    return boxes, samples
