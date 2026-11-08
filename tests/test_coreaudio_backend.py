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
