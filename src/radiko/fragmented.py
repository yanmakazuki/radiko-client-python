"""単一 AAC トラックの fragmented MP4。時刻は tfdt、失敗したら状態を捨てる。

復号そのものは PyAV である。箱の形、通し番号、tfdt の連続、PCM 長の一致は
Rust の `FragmentedAacDecoder` と同じ条件で拒否する。
"""

from __future__ import annotations

import av

from radiko.media import MAX_SEGMENT_BYTES, AacError

_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


class FragmentPcm:
    def __init__(
        self,
        decode_time: int,
        timescale: int,
        duration: int,
        samples: list[float],
        rate: int,
        channels: int,
    ):
        self.decode_time = decode_time
        self.timescale = timescale
        self.duration = duration
        self.samples = samples
        self.sample_rate = rate
        self.channels = channels
        self.frames = len(samples) // max(channels, 1)

    def __repr__(self) -> str:
        return f"FragmentPcm(time={self.decode_time}, frames={self.frames}, samples=[REDACTED])"


class _Atom:
    def __init__(self, kind: bytes, data: bytes, offset: int, header: int):
        self.kind = kind
        self.data = data
        self.offset = offset
        self.header = header


def _atoms(data: bytes) -> list[_Atom]:
    result = []
    at = 0
    while at < len(data):
        if len(result) == 8192:
            raise AacError("many")
        rest = data[at:]
        size = _word(rest, 0)
        kind = rest[4:8]
        if len(kind) != 4:
            raise AacError("container")
        if size == 0:
            size, header = len(rest), 8
        elif size == 1:
            size = (_word(rest, 8) << 32) | _word(rest, 12)
            header = 16
        else:
            header = 8
        if size < header or size > len(rest):
            raise AacError("container")
        result.append(_Atom(kind, rest[header:size], at, header))
        at += size
    return result


def _one(items: list[_Atom], kind: bytes) -> _Atom:
    found = [item for item in items if item.kind == kind]
    if len(found) != 1:
        raise AacError("container" if not found else "unsupported-container")
    return found[0]


def _word(data: bytes, at: int) -> int:
    chunk = data[at : at + 4]
    if len(chunk) != 4:
        raise AacError("container")
    return int.from_bytes(chunk, "big")


def _take(data: bytes, at: list[int]) -> int:
    value = _word(data, at[0])
    at[0] += 4
    return value


def _asc_config(asc: bytes) -> tuple[int, int]:
    if len(asc) < 2:
        raise AacError("config")
    bits = int.from_bytes(asc[:2], "big")
    object_type = (bits >> 11) & 0x1F
    rate_index = (bits >> 7) & 0xF
    channels = (bits >> 3) & 0xF
    if object_type != 2 or rate_index >= len(_RATES) or not 1 <= channels <= 7:
        raise AacError("config")
    return _RATES[rate_index], channels


def _decoder_specific(esds: bytes) -> bytes:
    if len(esds) < 4:
        raise AacError("config")
    body = esds[4:]

    def walk(blob: bytes) -> bytes | None:
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
        from radiko.media import validate_mp4

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
        version = tkhd[0]
        track = _word(tkhd, 20 if version == 1 else 12)
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
        self._codec = None
        self.next_time: int | None = None
        self.next_sequence: int | None = None

    def decode_pcm(self, data: bytes, cancelled=None) -> FragmentPcm:
        cancelled = cancelled or (lambda: False)
        try:
            result = self._decode(data, cancelled)
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
        from radiko.media import validate_mp4

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
        if self.next_sequence is not None and self.next_sequence != sequence:
            raise AacError("container")
        traf = _atoms(_one(children, b"traf").data)
        if len(traf) != 3:
            raise AacError("unsupported-container")
        tfhd = _one(traf, b"tfhd").data
        flags = _word(tfhd, 0)
        if flags & ~0x2003A or flags & 0x20000 == 0 or _word(tfhd, 4) != self.track:
            raise AacError("unsupported-container")
        at = [8]
        if flags & 2 and _take(tfhd, at) != 1:
            raise AacError("unsupported-container")
        default_duration = _take(tfhd, at) if flags & 8 else self.default_duration
        default_size = _take(tfhd, at) if flags & 16 else self.default_size
        if flags & 32:
            _take(tfhd, at)
        if at[0] != len(tfhd):
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
        cursor_at = [12]
        if flags & 4:
            _take(trun, cursor_at)
        packets = []
        duration = 0
        cursor = 0
        for _ in range(count):
            if cancelled():
                raise AacError("cancelled")
            dur = _take(trun, cursor_at) if flags & 0x100 else default_duration
            size = _take(trun, cursor_at) if flags & 0x200 else default_size
            if flags & 0x400:
                _take(trun, cursor_at)
            if dur == 0 or size == 0:
                raise AacError("container")
            duration += dur
            end = cursor + size
            piece = mdat.data[cursor:end]
            if len(piece) != size:
                raise AacError("container")
            packets.append(piece)
            cursor = end
        if cursor_at[0] != len(trun) or cursor != len(mdat.data):
            raise AacError("container")
        return {
            "decode_time": decode_time,
            "duration": duration,
            "next_time": decode_time + duration,
            "next_sequence": sequence + 1,
            "packets": packets,
        }

    def _decode(self, data: bytes, cancelled) -> FragmentPcm:
        media = self._read(data, cancelled)
        if self._codec is None:
            context = av.CodecContext.create("aac", "r")
            context.extradata = bytes(self.asc)
            context.sample_rate = self.sample_rate
            context.layout = "mono" if self.channels == 1 else "stereo"
            self._codec = context
        samples: list[float] = []
        for packet in media["packets"]:
            if cancelled():
                raise AacError("cancelled")
            for frame in self._codec.decode(av.Packet(packet)):
                plane = frame.to_ndarray()
                if plane.ndim == 1:
                    samples.extend(float(value) for value in plane)
                else:
                    samples.extend(float(value) for value in plane.reshape(-1, order="F"))
        channels = self.channels
        frames = len(samples) // channels
        if frames * self.timescale != media["duration"] * self.sample_rate:
            raise AacError("container")
        if cancelled():
            raise AacError("cancelled")
        self.next_time = media["next_time"]
        self.next_sequence = media["next_sequence"]
        return FragmentPcm(
            media["decode_time"],
            self.timescale,
            media["duration"],
            samples,
            self.sample_rate,
            channels,
        )


def box(kind: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def words(*values: int) -> bytes:
    return b"".join(int(value).to_bytes(4, "big") for value in values)


def synthetic_init(asc: bytes, timescale: int, default_duration: int, default_size: int) -> bytes:
    """実ペイロードを包む初期化セグメント。試験が使う。"""
    descriptor = bytes([0x05, len(asc)]) + asc
    decoder_config = (
        bytes([0x04, 13 + len(descriptor), 0x40, 0x15, 0, 0, 0]) + words(0, 0) + descriptor
    )
    es = bytes([0x03, 3 + len(decoder_config), 0, 1, 0]) + decoder_config
    esds = box(b"esds", words(0) + es)
    entry = (
        b"\x00" * 6
        + b"\x00\x01"
        + b"\x00" * 8
        + (1).to_bytes(2, "big")
        + (16).to_bytes(2, "big")
        + b"\x00\x00\x00\x00"
        + (timescale << 16).to_bytes(4, "big")
        + esds
    )
    stsd = box(b"stsd", words(0, 1) + box(b"mp4a", entry))
    stbl = box(
        b"stbl",
        stsd
        + box(b"stts", words(0, 0))
        + box(b"stsc", words(0, 0))
        + box(b"stsz", words(0, 0, 0))
        + box(b"stco", words(0, 0)),
    )
    mdhd = box(b"mdhd", words(0, 0, 0, timescale, 0) + b"\x55\xc4\x00\x00")
    mdia = box(b"mdia", mdhd + box(b"minf", stbl))
    tkhd = box(b"tkhd", words(0, 0, 0, 1))
    trak = box(b"trak", tkhd + mdia)
    trex = box(b"trex", words(0, 1, 1, default_duration, default_size, 0))
    moov = box(b"moov", trak + box(b"mvex", trex))
    ftyp = box(b"ftyp", b"isom" + words(0) + b"isom")
    return ftyp + moov


def synthetic_fragment(packets: list[bytes], decode_time: int, sequence: int) -> bytes:
    """moof+mdat。data_offset は moof の実長から置く。"""
    sizes = b"".join(len(packet).to_bytes(4, "big") for packet in packets)
    trun_payload = words(0x201, len(packets), 0) + sizes
    tfhd = box(b"tfhd", words(0x20000, 1))
    if decode_time <= 0xFFFFFFFF:
        tfdt = box(b"tfdt", words(0, decode_time))
    else:
        tfdt = box(b"tfdt", words(0x01000000, decode_time >> 32, decode_time & 0xFFFFFFFF))
    traf = box(b"traf", tfhd + tfdt + box(b"trun", trun_payload))
    moof = box(b"moof", box(b"mfhd", words(0, sequence)) + traf)
    offset = len(moof) + 8
    trun_at = moof.find(b"trun")
    moof = moof[: trun_at + 12] + offset.to_bytes(4, "big") + moof[trun_at + 16 :]
    return moof + box(b"mdat", b"".join(packets))
