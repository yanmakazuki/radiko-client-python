"""CoreAudio の呼び出し順。Linux では AudioToolbox が無く、機器は開かない。"""

from __future__ import annotations

import ctypes
import sys

import pytest

from radiko.coreaudio import FLOAT_PACKED, LINEAR_PCM, _description, inspect, library
from radiko.output import OutputError, host_backend


class _AudioQueue:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def AudioQueueNewOutput(self, fmt, callback, user, loop, mode, flags, out):
        self.calls.append("new")
        ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(7)
        return 0

    def AudioQueueDispose(self, queue, immediate):
        self.calls.append(("dispose", queue, bool(immediate)))
        return 0


def test_linux_does_not_load_audiotoolbox_or_claim_a_device():
    if sys.platform == "darwin":
        pytest.skip("macOS では欠落ライブラリの経路を確認できない")
    assert host_backend() == "alsa"
    assert library() is None
    with pytest.raises(OutputError) as raised:
        inspect(48000)
    assert raised.value.code == "none"


def test_fake_audioqueue_is_asked_for_float_stereo_and_disposed(monkeypatch):
    """機器は開かない。診断が AudioQueue を作り、形式を渡し、終わったら破棄する。"""
    backend = _AudioQueue()
    monkeypatch.setattr("radiko.coreaudio.library", lambda: backend)
    info = inspect(48000)
    assert info.name == "default"
    assert info.sample_format == "f32"
    assert info.channels == 2
    description = _description(48000, 2)
    assert description.mFormatID == LINEAR_PCM
    assert description.mFormatFlags == FLOAT_PACKED
    assert description.mSampleRate == 48000
    assert description.mChannelsPerFrame == 2
    assert description.mBitsPerChannel == 32
    assert backend.calls[0] == "new"
    assert backend.calls[-1][0] == "dispose"
    assert backend.calls[-1][2] is True


def test_playback_callback_stays_alive_until_dispose(monkeypatch):
    """AudioQueue のコールバックは Stop まで保持し、Dispose で手放す。機器は開かない。"""
    import threading

    from radiko.coreaudio import QueueBuffer, _callbacks, play
    from radiko.output import OutputInfo

    storage = ctypes.create_string_buffer(64)
    holder = QueueBuffer()
    holder.mAudioData = ctypes.cast(storage, ctypes.c_void_p)
    holder.mAudioDataBytesCapacity = 64

    class _Queue:
        def __init__(self) -> None:
            self.order: list[str] = []
            self.callback = None
            self.alive_at_stop = False

        def AudioQueueNewOutput(self, fmt, callback, user, loop, mode, flags, out):
            self.order.append("new")
            self.callback = callback
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(3)
            return 0

        def AudioQueueAllocateBuffer(self, queue, size, out):
            self.order.append("allocate")
            ctypes.cast(out, ctypes.POINTER(ctypes.POINTER(QueueBuffer)))[0] = ctypes.pointer(
                holder
            )
            return 0

        def AudioQueueEnqueueBuffer(self, queue, buffer, count, descriptions):
            self.order.append("enqueue")
            assert buffer.contents.mAudioDataByteSize > 0
            return 0

        def AudioQueueStart(self, queue, start_time):
            self.order.append("start")
            return 0

        def AudioQueueIsRunning(self, queue, running):
            self.order.append("running")
            running._obj.value = 0 if hasattr(running, "_obj") else None
            ctypes.cast(running, ctypes.POINTER(ctypes.c_uint32))[0] = 0
            return 0

        def AudioQueueStop(self, queue, immediate):
            self.order.append("stop")
            self.alive_at_stop = self.callback in _callbacks
            return 0

        def AudioQueueDispose(self, queue, immediate):
            self.order.append("dispose")
            return 0

    backend = _Queue()
    monkeypatch.setattr("radiko.coreaudio.library", lambda: backend)
    summary = play(
        [0.0, 0.0, 0.0, 0.0],
        48000,
        2,
        OutputInfo("default", 48000, "f32", 2, ""),
        threading.Event(),
    )
    assert summary.frames == 2
    assert backend.order[:4] == ["new", "allocate", "enqueue", "start"]
    assert "stop" in backend.order and backend.order[-1] == "dispose"
    assert backend.alive_at_stop
    assert backend.callback not in _callbacks
