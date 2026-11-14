"""単一 AAC トラックの fragmented MP4。時刻は tfdt、失敗したら状態を捨てる。

復号そのものは PyAV である。箱の形、通し番号、tfdt の連続、PCM 長の一致は
Rust の `FragmentedAacDecoder` と同じ条件で拒否する。
"""

from __future__ import annotations

import struct

import av
import numpy as np

from radiko.media import MAX_PCM_BYTES, MAX_SEGMENT_BYTES, AacError, validate_mp4

_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]
_WORD = struct.Struct(">I")


class FragmentPcm:
    def __init__(
        self,
        decode_time: int,
        timescale: int,
        duration: int,
        samples: np.ndarray,
        rate: int,
        channels: int,
        packets: int = 0,
    ):
        self.decode_time = decode_time
        self.timescale = timescale
        self.duration = duration
        self.samples = samples
        self.sample_rate = rate
        self.channels = channels
        self.packets = packets
        self.frames = len(samples) // max(channels, 1)

    def __repr__(self) -> str:
        return f"FragmentPcm(time={self.decode_time}, frames={self.frames}, samples=[REDACTED])"


class _Atom:
    def __init__(self, kind: bytes, data, offset: int, header: int):
        self.kind = kind
        self.data = data
        self.offset = offset
        self.header = header


def _atoms(data) -> list[_Atom]:
    view = memoryview(data)
    total = len(view)
    result: list[_Atom] = []
    at = 0
    while at < total:
        if len(result) == 8192:
            raise AacError("many")
        size = _word(view, at)
        kind = bytes(view[at + 4 : at + 8])
        if len(kind) != 4:
            raise AacError("container")
        if size == 0:
            size, header = total - at, 8
        elif size == 1:
            size = (_word(view, at + 8) << 32) | _word(view, at + 12)
            header = 16
        else:
            header = 8
        if size < header or size > total - at:
            raise AacError("container")
        result.append(_Atom(kind, view[at + header : at + size], at, header))
        at += size
    return result


def _one(items: list[_Atom], kind: bytes) -> _Atom:
    found = [item for item in items if item.kind == kind]
    if len(found) != 1:
        raise AacError("container" if not found else "unsupported-container")
    return found[0]


def _word(data, at: int) -> int:
    try:
        return _WORD.unpack_from(data, at)[0]
    except struct.error as error:
        raise AacError("container") from error


def _take(data, at: int) -> tuple[int, int]:
    return _word(data, at), at + 4


def _parse_tfhd(
    traf: list[_Atom], track: int, default_duration: int, default_size: int
) -> tuple[int, int]:
    """tfhd を検証し、(既定 duration, 既定 size) を返す。"""
    tfhd = _one(traf, b"tfhd").data
    flags = _word(tfhd, 0)
    if flags & ~0x2003A or flags & 0x20000 == 0 or _word(tfhd, 4) != track:
        raise AacError("unsupported-container")
    at = 8
    if flags & 2:
        selector, at = _take(tfhd, at)
        if selector != 1:
            raise AacError("unsupported-container")
    if flags & 8:
        default_duration, at = _take(tfhd, at)
    if flags & 16:
        default_size, at = _take(tfhd, at)
    if flags & 32:
        _, at = _take(tfhd, at)
    if at != len(tfhd):
        raise AacError("container")
    return default_duration, default_size


def _parse_tfdt(tfdt) -> int:
    """tfdt から decode time を返す。version 0 と 1 だけを許す。"""
    version = _word(tfdt, 0)
    if version == 0 and len(tfdt) == 8:
        return _word(tfdt, 4)
    if version == 0x01000000 and len(tfdt) == 12:
        return (_word(tfdt, 4) << 32) | _word(tfdt, 8)
    raise AacError("container")


def _asc_config(asc) -> tuple[int, int]:
    if len(asc) < 2:
        raise AacError("config")
    bits = int.from_bytes(asc[:2], "big")
    object_type = (bits >> 11) & 0x1F
    rate_index = (bits >> 7) & 0xF
    channels = (bits >> 3) & 0xF
    if object_type != 2 or rate_index >= len(_RATES) or not 1 <= channels <= 7:
        raise AacError("config")
    return _RATES[rate_index], channels


def _decoder_specific(esds) -> bytes | memoryview:
    if len(esds) < 4:
        raise AacError("config")
    body = esds[4:]

    def walk(blob) -> bytes | memoryview | None:
        index = 0
        while index < len(blob):
            tag = blob[index]
            index += 1
            length = 0
            for _ in range(4):
                if index >= len(blob):
                    raise AacError("config")
                byte = blob[index]
                index += 1
                length = (length << 7) | (byte & 0x7F)
                if byte < 128:
                    break
            chunk = blob[index : index + length]
            index += length
            if tag == 0x05:
                return chunk
            if tag in {0x03, 0x04}:
                nested = chunk[3:] if tag == 0x03 and len(chunk) >= 3 else chunk
                if tag == 0x04 and len(chunk) >= 13:
                    nested = chunk[13:]
                found = walk(nested)
                if found is not None:
                    return found
        return None

    found = walk(body)
    if not found:
        raise AacError("config")
    return found


class FragmentedAacDecoder:
    def __init__(self, init: bytes):
        if len(init) > 1024 * 1024:
            raise AacError("large")

        validate_mp4(init)
        top = _atoms(init)
        if len(top) != 2 or top[0].kind != b"ftyp" or top[1].kind != b"moov":
            raise AacError("unsupported-container")
        moov = _atoms(top[1].data)
        trak = _atoms(_one(moov, b"trak").data)
        if any(item.kind == b"edts" for item in trak):
            raise AacError("unsupported-container")
        mdia = _atoms(_one(trak, b"mdia").data)
        mdhd = _one(mdia, b"mdhd").data
        version = _word(mdhd, 0) >> 24
        timescale = _word(mdhd, 20 if version == 1 else 12)
        if timescale == 0:
            raise AacError("container")
        minf = _atoms(_one(mdia, b"minf").data)
        if any(item.kind == b"dinf" for item in minf):
            dinf = _atoms(_one(minf, b"dinf").data)
            dref = _one(dinf, b"dref").data
            if len(dref) < 8 or _word(dref, 0) != 0 or _word(dref, 4) != 1:
                raise AacError("unsupported-container")
            refs = _atoms(dref[8:])
            if len(refs) != 1 or refs[0].kind != b"url " or refs[0].data != b"\x00\x00\x00\x01":
                raise AacError("unsupported-container")
        stbl = _atoms(_one(minf, b"stbl").data)
        for kind in (b"stts", b"stsc", b"stco"):
            table = _one(stbl, kind).data
            if len(table) != 8 or _word(table, 0) != 0 or _word(table, 4) != 0:
                raise AacError("container")
        stsz = _one(stbl, b"stsz").data
        if len(stsz) != 12 or _word(stsz, 0) != 0 or _word(stsz, 8) != 0:
            raise AacError("container")
        stsd = _one(stbl, b"stsd").data
        if _word(stsd, 0) != 0 or _word(stsd, 4) != 1:
            raise AacError("unsupported-container")
        entries = _atoms(stsd[8:])
        if len(entries) != 1 or entries[0].kind != b"mp4a":
            raise AacError("codec")
        entry = entries[0].data
        if entry[6:8] != b"\x00\x01" or entry[8:16] != b"\x00" * 8:
            raise AacError("unsupported-container")
        description = _atoms(entry[28:])
        if any(item.kind not in {b"esds", b"btrt"} for item in description):
            raise AacError("unsupported-container")
        asc = _decoder_specific(_one(description, b"esds").data)
        rate, channels = _asc_config(asc)
        tkhd = _one(trak, b"tkhd").data
        if not tkhd or tkhd[0] not in {0, 1}:
            raise AacError("container")
        track = _word(tkhd, 20 if tkhd[0] == 1 else 12)
        if track == 0:
            raise AacError("container")
        trex = _one(_atoms(_one(moov, b"mvex").data), b"trex").data
        if len(trex) != 24 or _word(trex, 0) != 0 or _word(trex, 4) != track or _word(trex, 8) != 1:
            raise AacError("container")
        self.track = track
        self.timescale = timescale
        self.default_duration = _word(trex, 12)
        self.default_size = _word(trex, 16)
        self.asc = asc
        self.sample_rate = rate
        self.channels = channels
        self._codec: av.CodecContext | None = None
        self.next_time: int | None = None
        self.next_sequence: int | None = None

    def decode_pcm(self, data: bytes, cancelled=None, declared: str | None = None) -> FragmentPcm:
        cancelled = cancelled or (lambda: False)
        try:
            result = self._decode(data, cancelled, declared)
        except Exception:
            self._codec = None
            self.next_time = None
            self.next_sequence = None
            raise
        return result

    def _read(self, data: bytes, cancelled) -> dict:
        if cancelled():
            raise AacError("cancelled")
        if len(data) > MAX_SEGMENT_BYTES:
            raise AacError("large")

        validate_mp4(data)
        top = _atoms(data)
        moof = mdat = None
        if len(top) == 2 and top[0].kind == b"moof" and top[1].kind == b"mdat":
            moof, mdat = top
        elif (
            len(top) == 3
            and top[0].kind == b"styp"
            and top[1].kind == b"moof"
            and top[2].kind == b"mdat"
        ):
            moof, mdat = top[1], top[2]
        else:
            raise AacError("unsupported-container")
        children = _atoms(moof.data)
        if len(children) != 2:
            raise AacError("unsupported-container")
        mfhd = _one(children, b"mfhd").data
        if len(mfhd) != 8 or _word(mfhd, 0) != 0:
            raise AacError("container")
        sequence = _word(mfhd, 4)
        if sequence == 0xFFFFFFFF:
            raise AacError("container")
        if self.next_sequence is not None and self.next_sequence != sequence:
            raise AacError("container")
        traf = _atoms(_one(children, b"traf").data)
        if len(traf) != 3:
            raise AacError("unsupported-container")
        tfhd = _one(traf, b"tfhd").data
        flags = _word(tfhd, 0)
        if flags & ~0x2003A or flags & 0x20000 == 0 or _word(tfhd, 4) != self.track:
            raise AacError("unsupported-container")
        at = 8
        if flags & 2:
            selector, at = _take(tfhd, at)
            if selector != 1:
                raise AacError("unsupported-container")
        if flags & 8:
            default_duration, at = _take(tfhd, at)
        else:
            default_duration = self.default_duration
        if flags & 16:
            default_size, at = _take(tfhd, at)
        else:
            default_size = self.default_size
        if flags & 32:
            _, at = _take(tfhd, at)
        if at != len(tfhd):
            raise AacError("container")
        tfdt = _one(traf, b"tfdt").data
        version = _word(tfdt, 0)
        if version == 0 and len(tfdt) == 8:
            decode_time = _word(tfdt, 4)
        elif version == 0x01000000 and len(tfdt) == 12:
            decode_time = (_word(tfdt, 4) << 32) | _word(tfdt, 8)
        else:
            raise AacError("container")
        if self.next_time is not None and self.next_time != decode_time:
            raise AacError("container")
        trun = _one(traf, b"trun").data
        flags = _word(trun, 0)
        if flags & ~0x705 or flags & 1 == 0 or flags & 0x404 == 0x404:
            raise AacError("unsupported-container")
        count = _word(trun, 4)
        if count == 0:
            raise AacError("empty")
        if count > 512:
            raise AacError("many")
        offset = _word(trun, 8)
        if offset >= 0x80000000:
            offset = offset - 0x100000000
        if offset < 0 or moof.offset + offset != mdat.offset + mdat.header:
            raise AacError("container")
        cursor_at = 12
        if flags & 4:
            _, cursor_at = _take(trun, cursor_at)
        packets = []
        duration = 0
        cursor = 0
        for _ in range(count):
            if cancelled():
                raise AacError("cancelled")
            if flags & 0x100:
                dur, cursor_at = _take(trun, cursor_at)
            else:
                dur = default_duration
            if flags & 0x200:
                size, cursor_at = _take(trun, cursor_at)
            else:
                size = default_size
            if flags & 0x400:
                _, cursor_at = _take(trun, cursor_at)
            if dur == 0 or size == 0:
                raise AacError("container")
            duration += dur
            end = cursor + size
            piece = mdat.data[cursor:end]
            if len(piece) != size:
                raise AacError("container")
            packets.append(piece)
            cursor = end
        if cursor_at != len(trun) or cursor != len(mdat.data):
            raise AacError("container")
        next_time = decode_time + duration
        if next_time > 0xFFFFFFFFFFFFFFFF:
            raise AacError("container")
        return {
            "decode_time": decode_time,
            "duration": duration,
            "next_time": next_time,
            "next_sequence": sequence + 1,
            "packets": packets,
        }

    def _decode(self, data: bytes, cancelled, declared: str | None) -> FragmentPcm:
        media = self._read(data, cancelled)
        codec = self._codec
        if codec is None:
            codec = av.CodecContext.create("aac", "r")
            codec.extradata = bytes(self.asc)
            # PyAV 14.2 の型スタブには、実行時に存在するこの2属性が無い。
            codec.sample_rate = self.sample_rate  # type: ignore[attr-defined]
            codec.layout = "mono" if self.channels == 1 else "stereo"  # type: ignore[attr-defined]
            self._codec = codec
        samples: list[np.ndarray] = []
        rate = self.sample_rate
        channels = self.channels
        for packet in media["packets"]:
            if cancelled():
                raise AacError("cancelled")
            # PyAV 14.2 の型スタブは CodecContext.decode を宣言していない。
            for frame in codec.decode(av.Packet(packet)):  # type: ignore[attr-defined]
                plane = frame.to_ndarray()
                if plane.ndim == 1:
                    interleaved = plane.reshape(-1)
                    channels = 1
                else:
                    interleaved = plane.reshape(-1, order="F")
                    channels = plane.shape[0]
                rate = frame.sample_rate
                samples.append(np.ascontiguousarray(interleaved, dtype=np.float32))
        if not samples:
            raise AacError("empty")
        pcm = np.concatenate(samples) if len(samples) > 1 else samples[0]
        if pcm.size * 4 > MAX_PCM_BYTES:
            raise AacError("large")
        frames = pcm.size // max(channels, 1)
        if frames * self.timescale != media["duration"] * rate:
            raise AacError("container")
        sbr = rate > self.sample_rate
        if declared and any(
            part.strip() in {"mp4a.40.5", "mp4a.40.29"} for part in declared.split(",")
        ):
            if not sbr:
                raise AacError("sbr-missing")
        if cancelled():
            raise AacError("cancelled")
        self.next_time = media["next_time"]
        self.next_sequence = media["next_sequence"]
        return FragmentPcm(
            media["decode_time"],
            self.timescale,
            media["duration"],
            pcm,
            rate,
            channels,
            len(media["packets"]),
        )
