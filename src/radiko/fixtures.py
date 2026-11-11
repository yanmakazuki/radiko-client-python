"""試験用の合成データ。本番の再生経路からは使わない。

Rust 版の `player/fixtures.rs` と同じ境界で、実配信のバイト列ではない。
"""

from __future__ import annotations

import io

import av
import numpy as np


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


def encode_adts_sine(packets: int = 2, rate: int = 44100) -> bytes:
    """試験用の AAC-LC ADTS。実配信のバイト列ではない。"""
    output = io.BytesIO()
    container = av.open(output, mode="w", format="adts")
    stream = container.add_stream("aac", rate=rate)
    stream.layout = "mono"
    positions = np.arange(packets * 1024, dtype=np.float64)
    values = (0.2 * np.sin(2 * np.pi * 440 * positions / rate)).astype(np.float32)
    for index in range(packets):
        block = np.ascontiguousarray(values[index * 1024 : (index + 1) * 1024].reshape(1, 1024))
        frame = av.AudioFrame.from_ndarray(block, format="fltp", layout="mono")
        frame.sample_rate = rate
        frame.pts = index * 1024
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return output.getvalue()
