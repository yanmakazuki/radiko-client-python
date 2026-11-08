"""macOS の CoreAudio。AudioToolbox の AudioQueue で f32 を出す。

この環境は Linux なので、ここでは機器を開いていない。ライブラリが無いときは
「既定の音声出力機器がありません」になる。呼び出し順は偽のライブラリで確認する。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
from array import array

from radiko.output import OutputError, OutputInfo, OutputSummary

LINEAR_PCM = 0x6C70636D
FLOAT_PACKED = 1 | 8


class StreamDescription(ctypes.Structure):
    _fields_ = [
        ("mSampleRate", ctypes.c_double),
        ("mFormatID", ctypes.c_uint32),
        ("mFormatFlags", ctypes.c_uint32),
        ("mBytesPerPacket", ctypes.c_uint32),
        ("mFramesPerPacket", ctypes.c_uint32),
        ("mBytesPerFrame", ctypes.c_uint32),
        ("mChannelsPerFrame", ctypes.c_uint32),
        ("mBitsPerChannel", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


class QueueBuffer(ctypes.Structure):
    _fields_ = [
        ("mAudioDataBytesCapacity", ctypes.c_uint32),
        ("mAudioData", ctypes.c_void_p),
        ("mAudioDataByteSize", ctypes.c_uint32),
        ("mUserData", ctypes.c_void_p),
        ("mPacketDescriptionCapacity", ctypes.c_uint32),
        ("mPacketDescriptions", ctypes.c_void_p),
        ("mPacketDescriptionCount", ctypes.c_uint32),
    ]


_OUTPUT_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_callbacks: list = []


def library():
    """darwin で AudioToolbox が見つかったときだけ返す。"""
    if sys.platform != "darwin":
        return None
    path = ctypes.util.find_library("AudioToolbox")
    if not path:
        return None
    loaded = ctypes.CDLL(path)
    _bind(loaded)
    return loaded


def inspect(rate: int, opened=None) -> OutputInfo:
    own = opened is None
    handle = opened or _open(rate, 2)
    try:
        if handle is None:
            raise OutputError("none")
        return OutputInfo("default", rate, "f32", 2, "")
    finally:
        if own and handle is not None:
            _dispose(handle)


def play(samples: list[float], rate: int, channels: int, info: OutputInfo, stop) -> OutputSummary:
    handle = _open(rate, info.channels or channels)
    if handle is None:
        raise OutputError("none")
    lib, queue = handle
    done = threading.Event()
    frames = len(samples) // channels
    raw = array("f", samples)
    try:
        size = len(raw) * raw.itemsize
        buffer_ref = ctypes.POINTER(QueueBuffer)()
        status = lib.AudioQueueAllocateBuffer(queue, max(size, 4), ctypes.byref(buffer_ref))
        if status != 0 or not buffer_ref:
            raise OutputError("device")
        destination = ctypes.cast(buffer_ref.contents.mAudioData, ctypes.POINTER(ctypes.c_char))
        ctypes.memmove(destination, raw.tobytes(), size)
        buffer_ref.contents.mAudioDataByteSize = size
        status = lib.AudioQueueEnqueueBuffer(queue, buffer_ref, 0, None)
        if status != 0:
            raise OutputError("device")
        status = lib.AudioQueueStart(queue, None)
        if status != 0:
            raise OutputError("device")
        while not done.wait(0.01):
            if stop.is_set():
                break
            if _drained(lib, queue):
                break
        lib.AudioQueueStop(queue, True)
    finally:
        _dispose(handle)
    return OutputSummary(frames, rate, channels, "f32", 0)


def _drained(lib, queue) -> bool:
    running = ctypes.c_uint32()
    status = lib.AudioQueueIsRunning(queue, ctypes.byref(running))
    return status == 0 and running.value == 0


def _open(rate: int, channels: int, lib=None):
    lib = library() if lib is None else lib
    if lib is None:
        return None
    _bind(lib)
    description = _description(rate, channels)
    queue = ctypes.c_void_p()
    callback = _OUTPUT_CALLBACK(lambda *_args: None)
    _callbacks.append(callback)
    status = lib.AudioQueueNewOutput(
        ctypes.byref(description),
        callback,
        None,
        None,
        None,
        0,
        ctypes.byref(queue),
    )
    if status != 0 or not queue.value:
        return None
    return lib, queue


def _dispose(handle) -> None:
    lib, queue = handle
    lib.AudioQueueDispose(queue, True)


def _description(rate: int, channels: int) -> StreamDescription:
    description = StreamDescription()
    description.mSampleRate = rate
    description.mFormatID = LINEAR_PCM
    description.mFormatFlags = FLOAT_PACKED
    description.mFramesPerPacket = 1
    description.mBitsPerChannel = 32
    description.mChannelsPerFrame = channels
    description.mBytesPerFrame = 4 * channels
    description.mBytesPerPacket = 4 * channels
    return description


def _bind(lib) -> None:
    signatures = {
        "AudioQueueNewOutput": (
            [
                ctypes.POINTER(StreamDescription),
                _OUTPUT_CALLBACK,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_int32,
        ),
        "AudioQueueAllocateBuffer": (
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(QueueBuffer))],
            ctypes.c_int32,
        ),
        "AudioQueueEnqueueBuffer": (
            [ctypes.c_void_p, ctypes.POINTER(QueueBuffer), ctypes.c_uint32, ctypes.c_void_p],
            ctypes.c_int32,
        ),
        "AudioQueueStart": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int32),
        "AudioQueueStop": ([ctypes.c_void_p, ctypes.c_bool], ctypes.c_int32),
        "AudioQueueDispose": ([ctypes.c_void_p, ctypes.c_bool], ctypes.c_int32),
        "AudioQueueIsRunning": (
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int32,
        ),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(lib, name, None)
        if function is not None:
            _signature(function, argtypes, restype)


def _signature(function, argtypes, restype) -> None:
    if hasattr(function, "argtypes"):
        function.argtypes = argtypes
        function.restype = restype
