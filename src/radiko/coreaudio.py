"""macOS の CoreAudio。AudioToolbox の AudioQueue で f32 を出す。

既定出力機器の名前・対応レート・f32 のチャンネルは AudioObject から読む。
ライブラリやプロパティが読めないときは機器なしとして扱い、対応宣言をしない。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
import time

import numpy as np

from radiko.output import OutputError, OutputInfo, OutputSummary

LINEAR_PCM = 0x6C70636D
FLOAT_PACKED = 1 | 8
CF_STRING_ENCODING_UTF8 = 0x08000100


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


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


class AudioValueRange(ctypes.Structure):
    _fields_ = [
        ("mMinimum", ctypes.c_double),
        ("mMaximum", ctypes.c_double),
    ]


class AudioStreamRangedDescription(ctypes.Structure):
    _fields_ = [
        ("mFormat", StreamDescription),
        ("mSampleRateRange", AudioValueRange),
    ]


_OUTPUT_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_callbacks: list = []


def _fourcc(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


SYSTEM_OBJECT = 1
SCOPE_GLOBAL = _fourcc("glob")
DEFAULT_OUTPUT_DEVICE = _fourcc("dOut")
DEVICE_NAME = _fourcc("lnam")
NOMINAL_SAMPLE_RATES = _fourcc("nsr#")
DEVICE_STREAMS = _fourcc("stm#")
STREAM_AVAILABLE_FORMATS = _fourcc("pft?")
STREAM_PHYSICAL_FORMAT = _fourcc("pft ")
AUDIO_QUEUE_IS_RUNNING = _fourcc("aqrn")


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
    device = _default_output()
    if device is None:
        raise OutputError("none")
    name, nominal, formats = device
    channels = 2 if _supports(rate, 2, formats) else 1 if _supports(rate, 1, formats) else None
    if channels is not None:
        return OutputInfo(name, rate, "f32", channels, "")
    mismatch = _mismatch(rate, nominal, formats)
    return OutputInfo(name, rate, None, None, mismatch)


class Stream:
    """1つの AudioQueue を複数チャンクで使い回す。"""

    def __init__(self, rate: int, channels: int, stop, step_hook=None) -> None:
        self.rate = rate
        self.channels = channels
        self.stop = stop or threading.Event()
        self.step_hook = step_hook
        self.handle = _open(rate, channels)
        if self.handle is None:
            raise OutputError("none")
        self.lib, self.queue, self.callback = self.handle
        self.started = False
        self.started_at = 0.0
        self.queued_frames = 0
        self.played_frames = 0
        self.closed = False

    def write(self, samples, volume: int = 100) -> None:
        if self.closed or self.stop.is_set():
            return
        array = np.asarray(samples, dtype=np.float32).reshape(-1)
        gain = max(0, min(volume, 100)) / 100
        if gain != 1.0:
            array = array * gain
        frames = array.size // self.channels
        if frames <= 0:
            return
        payload = np.ascontiguousarray(array, dtype=np.float32)
        size = payload.nbytes
        buffer_ref = ctypes.POINTER(QueueBuffer)()
        status = self.lib.AudioQueueAllocateBuffer(
            self.queue, max(size, 4), ctypes.byref(buffer_ref)
        )
        if status != 0 or not buffer_ref:
            raise OutputError("device")
        destination = ctypes.cast(buffer_ref.contents.mAudioData, ctypes.POINTER(ctypes.c_char))
        ctypes.memmove(destination, payload.ctypes.data, size)
        buffer_ref.contents.mAudioDataByteSize = size
        status = self.lib.AudioQueueEnqueueBuffer(self.queue, buffer_ref, 0, None)
        if status != 0:
            raise OutputError("device")
        if not self.started:
            status = self.lib.AudioQueueStart(self.queue, None)
            if status != 0:
                raise OutputError("device")
            self.started = True
            self.started_at = time.monotonic()
        self.queued_frames += frames
        self._wait(self.started_at + self.queued_frames / self.rate - 0.25)

    def _wait(self, deadline: float) -> None:
        while True:
            if self.step_hook is not None:
                self.step_hook(self.queued_frames)
            if self.stop.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.01, remaining))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.started:
                if not self.stop.is_set():
                    self._wait(self.started_at + self.queued_frames / self.rate)
                elapsed = time.monotonic() - self.started_at
                self.played_frames = (
                    self.queued_frames
                    if not self.stop.is_set()
                    else min(self.queued_frames, max(0, int(elapsed * self.rate)))
                )
                self.lib.AudioQueueStop(self.queue, True)
        finally:
            _dispose(self.handle)


def play(
    samples,
    rate: int,
    channels: int,
    info: OutputInfo,
    stop,
    step_hook=None,
) -> OutputSummary:
    stream = Stream(rate, info.channels or channels, stop, step_hook)
    try:
        stream.write(samples)
        stream.close()
    except Exception:
        stream.close()
        raise
    return OutputSummary(stream.played_frames, rate, stream.channels, "f32", 0)


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
        _forget(callback)
        return None
    return lib, queue, callback


def _default_output():
    if sys.platform != "darwin":
        return None
    coreaudio = _load("CoreAudio")
    corefoundation = _load("CoreFoundation")
    if coreaudio is None or corefoundation is None:
        return None
    try:
        _bind(coreaudio)
        corefoundation.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        corefoundation.CFStringGetCString.restype = ctypes.c_bool
        corefoundation.CFRelease.argtypes = [ctypes.c_void_p]
        corefoundation.CFRelease.restype = None
        device = _default_device(coreaudio)
        if device is None:
            return None
        name = _device_name(coreaudio, corefoundation, device)
        nominal = _nominal_rates(coreaudio, device)
        formats = _stream_formats(coreaudio, device, nominal)
        if name is None or nominal is None or formats is None:
            return None
        return name, nominal, formats
    except (AttributeError, OSError, ValueError):
        return None


def _load(name: str):
    path = ctypes.util.find_library(name)
    if not path:
        return None
    return ctypes.CDLL(path)


def _address(selector: int, scope: int = SCOPE_GLOBAL, element: int = 0):
    return AudioObjectPropertyAddress(selector, scope, element)


def _get_data(function, object_id: int, address, size: int):
    data = (ctypes.c_char * size)()
    used = ctypes.c_uint32(size)
    status = function(object_id, ctypes.byref(address), 0, None, ctypes.byref(used), data)
    if status != 0 or used.value > size:
        return None
    return data.raw[: used.value]


def _get_size(function, object_id: int, address) -> int | None:
    size = ctypes.c_uint32()
    status = function(object_id, ctypes.byref(address), 0, None, ctypes.byref(size))
    if status != 0:
        return None
    return size.value


def _default_device(coreaudio) -> int | None:
    raw = _get_data(
        coreaudio.AudioObjectGetPropertyData,
        SYSTEM_OBJECT,
        _address(DEFAULT_OUTPUT_DEVICE),
        ctypes.sizeof(ctypes.c_uint32),
    )
    if raw is None or len(raw) != ctypes.sizeof(ctypes.c_uint32):
        return None
    device = int.from_bytes(raw, sys.byteorder)
    return device or None


def _device_name(coreaudio, corefoundation, device: int) -> str | None:
    address = _address(DEVICE_NAME)
    size = _get_size(coreaudio.AudioObjectGetPropertyDataSize, device, address)
    if not size or size < ctypes.sizeof(ctypes.c_void_p):
        return None
    pointer = ctypes.c_void_p()
    used = ctypes.c_uint32(ctypes.sizeof(pointer))
    status = coreaudio.AudioObjectGetPropertyData(
        device, ctypes.byref(address), 0, None, ctypes.byref(used), ctypes.byref(pointer)
    )
    if status != 0 or not pointer.value:
        return None
    try:
        buffer = ctypes.create_string_buffer(512)
        copied = corefoundation.CFStringGetCString(
            pointer, buffer, len(buffer), CF_STRING_ENCODING_UTF8
        )
        if not copied:
            return None
        return buffer.value.decode("utf-8", "replace")
    finally:
        corefoundation.CFRelease(pointer)


def _nominal_rates(coreaudio, device: int) -> list[float] | None:
    address = _address(NOMINAL_SAMPLE_RATES)
    size = _get_size(coreaudio.AudioObjectGetPropertyDataSize, device, address)
    if size is None:
        return None
    count = size // ctypes.sizeof(AudioValueRange)
    if count <= 0:
        return []
    raw = _get_data(
        coreaudio.AudioObjectGetPropertyData,
        device,
        address,
        count * ctypes.sizeof(AudioValueRange),
    )
    if raw is None:
        return None
    ranges = (AudioValueRange * count).from_buffer_copy(raw)
    rates: list[float] = []
    for item in ranges:
        rates.append(item.mMinimum)
        if item.mMaximum != item.mMinimum:
            rates.append(item.mMaximum)
    return sorted({rate for rate in rates if rate > 0})


def _stream_formats(
    coreaudio, device: int, nominal: list[float]
) -> list[tuple[float, float, int]] | None:
    address = _address(DEVICE_STREAMS)
    size = _get_size(coreaudio.AudioObjectGetPropertyDataSize, device, address)
    if size is None:
        return None
    count = size // ctypes.sizeof(ctypes.c_uint32)
    if count == 0:
        return []
    raw = _get_data(
        coreaudio.AudioObjectGetPropertyData,
        device,
        address,
        count * ctypes.sizeof(ctypes.c_uint32),
    )
    if raw is None:
        return None
    streams = (ctypes.c_uint32 * count).from_buffer_copy(raw)
    formats: list[tuple[float, float, int]] = []
    for stream in streams:
        stream_address = _address(STREAM_AVAILABLE_FORMATS)
        stream_size = _get_size(coreaudio.AudioObjectGetPropertyDataSize, stream, stream_address)
        items = []
        if stream_size:
            item_size = ctypes.sizeof(AudioStreamRangedDescription)
            item_count = stream_size // item_size
            raw_items = _get_data(
                coreaudio.AudioObjectGetPropertyData,
                stream,
                stream_address,
                item_count * item_size,
            )
            if raw_items is not None and item_count:
                items = list(
                    (AudioStreamRangedDescription * item_count).from_buffer_copy(raw_items)
                )
        if items:
            for item in items:
                fmt = item.mFormat
                if fmt.mFormatID == LINEAR_PCM and fmt.mFormatFlags & 1:
                    if fmt.mChannelsPerFrame in {1, 2}:
                        formats.append(
                            (
                                item.mSampleRateRange.mMinimum,
                                item.mSampleRateRange.mMaximum,
                                int(fmt.mChannelsPerFrame),
                            )
                        )
            continue
        if _float_physical_format(coreaudio, stream):
            for rate in nominal:
                formats.extend([(rate, rate, 1), (rate, rate, 2)])
    return formats


def _float_physical_format(coreaudio, stream: int) -> bool:
    address = _address(STREAM_PHYSICAL_FORMAT)
    raw = _get_data(
        coreaudio.AudioObjectGetPropertyData, stream, address, ctypes.sizeof(StreamDescription)
    )
    if raw is None or len(raw) < ctypes.sizeof(StreamDescription):
        return False
    description = StreamDescription.from_buffer_copy(raw)
    return description.mFormatID == LINEAR_PCM and bool(description.mFormatFlags & 1)


def _supports(rate: float, channels: int, formats) -> bool:
    return any(
        item_channels == channels and minimum <= rate <= maximum
        for minimum, maximum, item_channels in formats
    )


def _mismatch(rate: int, nominal: list[float], formats) -> str:
    supported = []
    for candidate in nominal:
        channels = (
            2 if _supports(candidate, 2, formats) else 1 if _supports(candidate, 1, formats) else 0
        )
        if channels:
            text = f"{candidate:g}Hz・f32・{channels}ch"
            if text not in supported:
                supported.append(text)
    range_text = "、".join(supported) if supported else "なし"
    return (
        f"必要な形式は{rate}Hzのmonoまたはstereoのf32・i16・i32です。機器の対応範囲: {range_text}"
    )


def _forget(callback) -> None:
    if callback in _callbacks:
        _callbacks.remove(callback)


def _dispose(handle) -> None:
    lib, queue = handle[0], handle[1]
    lib.AudioQueueDispose(queue, True)
    if len(handle) > 2:
        _forget(handle[2])


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
        "AudioQueueGetProperty": (
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int32,
        ),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(lib, name, None)
        if function is not None:
            _signature(function, argtypes, restype)
    for name, argtypes, restype in (
        (
            "AudioObjectGetPropertyData",
            [
                ctypes.c_uint32,
                ctypes.POINTER(AudioObjectPropertyAddress),
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        ),
        (
            "AudioObjectGetPropertyDataSize",
            [
                ctypes.c_uint32,
                ctypes.POINTER(AudioObjectPropertyAddress),
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int32,
        ),
    ):
        function = getattr(lib, name, None)
        if function is not None:
            _signature(function, argtypes, restype)


def _signature(function, argtypes, restype) -> None:
    if hasattr(function, "argtypes"):
        function.argtypes = argtypes
        function.restype = restype
