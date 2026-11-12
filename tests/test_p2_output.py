"""P2（Issue #18）: 出力バックエンドの境界。実機は開かず、偽のライブラリで見る。"""

from __future__ import annotations

import ctypes
import threading

import numpy as np
import pytest

import radiko.coreaudio as coreaudio
import radiko.output as output
from radiko.output import OutputError


def _formats(*entries) -> list[tuple]:
    return list(entries)


def test_device_name_drops_control_characters_and_is_truncated(monkeypatch):
    raw = "Sp\x00e\x1bakers " + "あ" * 300
    monkeypatch.setattr(
        coreaudio,
        "_default_output",
        lambda: (raw, [48000.0], _formats((48000.0, 48000.0, 2))),
    )
    info = coreaudio.inspect(48000)
    assert "\x00" not in info.name
    assert "\x1b" not in info.name
    assert info.name.startswith("Speakers ")
    assert len(info.name) == 256
    assert info.channels == 2


def test_mismatch_lists_every_offered_format_not_only_float(monkeypatch):
    formats = _formats(
        (44100.0, 44100.0, 2, "f32"),
        (44100.0, 44100.0, 1, "i16"),
        (48000.0, 96000.0, 2, "i32"),
    )
    monkeypatch.setattr(coreaudio, "_default_output", lambda: ("Dev", [44100.0], formats))
    info = coreaudio.inspect(48000)
    assert info.sample_format is None
    assert info.channels is None
    assert "44100Hz・f32・2ch" in info.mismatch
    assert "44100Hz・i16・1ch" in info.mismatch
    assert "48000〜96000Hz・i32・2ch" in info.mismatch


def test_mismatch_caps_the_offered_list_at_twelve(monkeypatch):
    formats = _formats(
        *[(float(8000 + index), float(8000 + index), 2, "f32") for index in range(13)]
    )
    monkeypatch.setattr(coreaudio, "_default_output", lambda: ("Dev", [], formats))
    info = coreaudio.inspect(48000)
    assert info.mismatch.endswith("、ほか")
    assert info.mismatch.count("Hz・") == 12


class _FrozenQueue:
    """最初の1回だけコールバックを返し、その後は AudioQueue が止まった偽装。"""

    def __init__(self, fire_once: bool = True) -> None:
        self.callback = None
        self.enqueued = 0
        self.fire_once = fire_once
        # 偽のバッファは Python 側で生かしておく（C 側はアドレスしか持たない）。
        self.buffers: list[coreaudio.QueueBuffer] = []
        self.storages: list[ctypes.Array] = []

    def AudioQueueNewOutput(self, fmt, callback, user, loop, mode, flags, out):
        self.callback = callback
        ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(3)
        return 0

    def AudioQueueAllocateBuffer(self, queue, size, out):
        storage = ctypes.create_string_buffer(max(size, 4))
        holder = coreaudio.QueueBuffer()
        holder.mAudioData = ctypes.cast(storage, ctypes.c_void_p)
        holder.mAudioDataBytesCapacity = len(storage)
        self.buffers.append(holder)
        self.storages.append(storage)
        ctypes.cast(out, ctypes.POINTER(ctypes.POINTER(coreaudio.QueueBuffer)))[0] = ctypes.pointer(
            holder
        )
        return 0

    def AudioQueueEnqueueBuffer(self, queue, buffer, count, descriptions):
        self.enqueued += 1
        if self.fire_once and self.enqueued == 1:
            self.callback(None, queue, buffer)
        return 0

    def AudioQueueStart(self, queue, start_time):
        return 0

    def AudioQueueStop(self, queue, immediate):
        return 0

    def AudioQueueDispose(self, queue, immediate):
        return 0


class _Clock:
    now = 0.0

    @classmethod
    def monotonic(cls) -> float:
        return cls.now

    @classmethod
    def sleep(cls, seconds: float) -> None:
        cls.now += seconds


def test_frozen_audioqueue_callbacks_raise_stalled(monkeypatch):
    backend = _FrozenQueue()
    monkeypatch.setattr("radiko.coreaudio.library", lambda: backend)
    monkeypatch.setattr("radiko.coreaudio.time", _Clock)
    stream = coreaudio.Stream(48000, 2, threading.Event())
    with pytest.raises(OutputError) as raised:
        stream.write(np.zeros(48000 * 2 * 5, dtype=np.float32))
    assert raised.value.code == "stalled"
    # 後片付けでは例外を重ねず、完了した分だけを再生済みとして残す。
    stream.close()
    assert stream.played_frames == 48000
    assert stream.played_frames < stream.queued_frames


def test_never_completed_buffers_stall_after_the_grace_period(monkeypatch):
    backend = _FrozenQueue(fire_once=False)
    monkeypatch.setattr("radiko.coreaudio.library", lambda: backend)
    monkeypatch.setattr("radiko.coreaudio.time", _Clock)
    stream = coreaudio.Stream(48000, 2, threading.Event())
    # 完了が一度も無いまま 3.25 秒（先行分0.25秒を含む）たったら止まったとみなす。
    stream.write(np.zeros(48000 * 2 * 3, dtype=np.float32))
    with pytest.raises(OutputError) as raised:
        stream.write(np.zeros(48000 * 2, dtype=np.float32))
    assert raised.value.code == "stalled"


class _AlsaFake:
    """部分書き込み → XRUN → 回復 → 完了、を順に返す libasound の偽装。"""

    def __init__(self, eagain: int = 0) -> None:
        self.writes: list[int] = []
        self.recovers: list[tuple[int, int]] = []
        self.phase = 0
        self.eagain = eagain

    def snd_pcm_writei(self, handle, pointer, frames):
        self.phase += 1
        if self.phase <= self.eagain:
            return -11
        self.writes.append(frames)
        if len(self.writes) == 1:
            return frames // 2
        if len(self.writes) == 2:
            return -32
        return frames

    def snd_pcm_recover(self, handle, err, silent):
        self.recovers.append((err, silent))
        return 0

    def snd_pcm_prepare(self, handle):
        return 0


def test_write_pcm_loops_partial_writes_and_recovers_from_xrun():
    library = _AlsaFake()
    written, underruns = output._write_pcm(
        (library, object()), np.ones(960, dtype=np.float32), "f32", 1
    )
    assert written == 960
    assert library.writes == [960, 480, 480]
    assert library.recovers == [(-32, 1)]
    assert underruns == 480


def test_write_pcm_gives_up_after_the_eagain_budget():
    library = _AlsaFake(eagain=output.MAX_WRITE_RETRIES + 1)
    with pytest.raises(OutputError) as raised:
        output._write_pcm((library, object()), np.ones(96, dtype=np.float32), "f32", 1)
    assert raised.value.code == "device"


def test_alsa_stream_counts_underrun_frames_in_the_summary(monkeypatch):
    library = _AlsaFake()
    monkeypatch.setattr(output, "null_sink_enabled", lambda: False)
    monkeypatch.setattr(output, "host_backend", lambda: "alsa")
    monkeypatch.setattr(output, "_open_default", lambda: (library, object()))
    monkeypatch.setattr(output, "_select_format", lambda opened, rate: ("f32", 2))
    monkeypatch.setattr(output, "_close", lambda opened: None)
    stream = output.open_stream(48000, 2, threading.Event())
    stream.write(np.zeros(960 * 2, dtype=np.float32))
    stream.close()
    summary = stream.summary()
    assert summary.frames == 960
    assert summary.underrun_frames == 480
    assert library.recovers == [(-32, 1)]


def test_write_pcm_without_a_recover_symbol_falls_back_to_prepare():
    class _Library:
        def __init__(self):
            self.prepared = 0

        def snd_pcm_writei(self, handle, pointer, frames):
            return -32 if self.prepared == 0 else frames

        def snd_pcm_prepare(self, handle):
            self.prepared += 1
            return 0

    library = _Library()
    written, underruns = output._write_pcm(
        (library, object()), np.ones(64, dtype=np.float32), "f32", 1
    )
    assert written == 64
    assert underruns == 64
    assert library.prepared == 1


class _HealthyQueue(_FrozenQueue):
    """毎回の enqueue で完了を返す、正常な AudioQueue の偽装。"""

    def AudioQueueEnqueueBuffer(self, queue, buffer, count, descriptions):
        self.enqueued += 1
        self.callback(None, queue, buffer)
        return 0


def test_long_write_is_split_so_completions_keep_it_alive(monkeypatch):
    backend = _HealthyQueue()
    monkeypatch.setattr("radiko.coreaudio.library", lambda: backend)
    monkeypatch.setattr("radiko.coreaudio.time", _Clock)
    stream = coreaudio.Stream(48000, 2, threading.Event())
    stream.write(np.zeros(48000 * 2 * 5, dtype=np.float32))
    assert backend.enqueued == 5  # 1秒ずつなら完了通知が3秒以上空かない
    stream.close()
    assert stream.played_frames == 48000 * 5


def test_idle_gap_between_writes_is_not_a_stall(monkeypatch):
    backend = _FrozenQueue()
    monkeypatch.setattr("radiko.coreaudio.library", lambda: backend)
    monkeypatch.setattr("radiko.coreaudio.time", _Clock)
    stream = coreaudio.Stream(48000, 2, threading.Event())
    stream.write(np.zeros(48000 * 2, dtype=np.float32))
    _Clock.now += 5.0  # 一時停止して何も書かない
    stream.write(np.zeros(48000 * 2, dtype=np.float32))
    assert backend.enqueued == 2
    stream.close()


def test_format_label_uses_the_signed_integer_flag():
    from radiko.coreaudio import LINEAR_PCM, StreamDescription, _format_label

    def label(flags: int, bits: int) -> str | None:
        fmt = StreamDescription()
        fmt.mFormatID = LINEAR_PCM
        fmt.mFormatFlags = flags
        fmt.mBitsPerChannel = bits
        return _format_label(fmt)

    assert label(0x0C, 16) == "i16"  # signed + packed
    assert label(0x0C, 32) == "i32"
    assert label(0x08, 16) == "u16"  # packed だけなら unsigned
    assert label(1, 32) == "f32"
    assert label(2, 16) == "u16"  # big-endian は signed ではない


def test_mismatch_keeps_multichannel_offers(monkeypatch):
    monkeypatch.setattr(
        coreaudio, "_default_output", lambda: ("Dev", [48000.0], [(48000.0, 48000.0, 6)])
    )
    info = coreaudio.inspect(44100)
    assert info.channels is None
    assert "48000Hz・f32・6ch" in info.mismatch
    assert coreaudio._supports(48000.0, 2, [(48000.0, 48000.0, 6)]) is False


def test_stream_formats_keeps_multichannel_declarations():
    from radiko.coreaudio import (
        DEVICE_STREAMS,
        LINEAR_PCM,
        STREAM_AVAILABLE_FORMATS,
        AudioStreamRangedDescription,
        StreamDescription,
    )

    description = StreamDescription()
    description.mFormatID = LINEAR_PCM
    description.mFormatFlags = 1
    description.mChannelsPerFrame = 6
    description.mBitsPerChannel = 32
    item = AudioStreamRangedDescription()
    item.mFormat = description
    item.mSampleRateRange.mMinimum = 48000.0
    item.mSampleRateRange.mMaximum = 48000.0

    class _CoreAudio:
        def AudioObjectGetPropertyDataSize(self, obj, address, qualifier, data, size):
            selector = address._obj.mSelector
            if selector == DEVICE_STREAMS:
                amount = ctypes.sizeof(ctypes.c_uint32)
            elif selector == STREAM_AVAILABLE_FORMATS:
                amount = ctypes.sizeof(item)
            else:
                return -1
            ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32))[0] = amount
            return 0

        def AudioObjectGetPropertyData(self, obj, address, qualifier, data, used, out):
            selector = address._obj.mSelector
            if selector == DEVICE_STREAMS:
                ctypes.cast(out, ctypes.POINTER(ctypes.c_uint32))[0] = 7
            elif selector == STREAM_AVAILABLE_FORMATS:
                ctypes.memmove(out, ctypes.byref(item), ctypes.sizeof(item))
            else:
                return -1
            return 0

    formats = coreaudio._stream_formats(_CoreAudio(), 5, [48000.0])
    assert (48000.0, 48000.0, 6, "f32") in formats


def test_device_name_buffer_holds_256_multibyte_characters():
    long_name = "あ" * 300
    encoded = long_name.encode("utf-8")
    seen = {}

    class _CoreAudio:
        def AudioObjectGetPropertyDataSize(self, device, address, qualifier, data, size):
            ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.sizeof(ctypes.c_void_p)
            return 0

        def AudioObjectGetPropertyData(self, device, address, qualifier, data, used, out):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(11)
            return 0

    class _CoreFoundation:
        def CFStringGetCString(self, pointer, buffer, size, encoding):
            seen["size"] = size
            if len(encoded) + 1 > size:
                return False
            ctypes.memmove(buffer, encoded + b"\x00", len(encoded) + 1)
            return True

        def CFRelease(self, pointer):
            return None

    name = coreaudio._device_name(_CoreAudio(), _CoreFoundation(), 5)
    assert seen["size"] >= 256 * 4 + 1
    assert name == long_name


class _RefusingQueue(_FrozenQueue):
    """エンキューだけ失敗する AudioQueue。確保済みバッファの解放を数える。"""

    def __init__(self) -> None:
        super().__init__()
        self.freed = 0

    def AudioQueueEnqueueBuffer(self, queue, buffer, count, descriptions):
        return -1

    def AudioQueueFreeBuffer(self, queue, buffer):
        self.freed += 1
        return 0


def test_enqueue_failure_returns_the_allocated_buffer(monkeypatch):
    backend = _RefusingQueue()
    monkeypatch.setattr("radiko.coreaudio.library", lambda: backend)
    monkeypatch.setattr("radiko.coreaudio.time", _Clock)
    stream = coreaudio.Stream(48000, 2, threading.Event())
    with pytest.raises(OutputError) as raised:
        stream.write(np.zeros(48000 * 2, dtype=np.float32))
    assert raised.value.code == "device"
    assert backend.freed == 1
    assert stream.outstanding == 0


def test_alsa_stream_reports_a_mismatch_without_reopening_the_device(monkeypatch):
    opens: list[int] = []

    def fake_open():
        opens.append(1)
        return object(), object()

    monkeypatch.setattr(output, "null_sink_enabled", lambda: False)
    monkeypatch.setattr(output, "host_backend", lambda: "alsa")
    monkeypatch.setattr(output, "_open_default", fake_open)
    monkeypatch.setattr(output, "_select_format", lambda opened, rate: None)
    monkeypatch.setattr(output, "_close", lambda opened: None)
    with pytest.raises(OutputError) as raised:
        output.open_stream(48000, 2, threading.Event())
    assert raised.value.code == "device"
    assert raised.value.message == (
        "必要な形式は48000Hzのmonoまたはstereoのf32・i16・i32です。機器の対応範囲: なし"
    )
    assert len(opens) == 1


def test_silence_buffer_is_reused_instead_of_allocated_per_pause():
    first = output._silence(96000)
    second = output._silence(96000)
    assert first.size == 96000
    assert np.shares_memory(first, second)
    assert not first.any()
