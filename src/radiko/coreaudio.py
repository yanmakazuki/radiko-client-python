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
import unicodedata

import numpy as np

from radiko.audio import OutputError, OutputInfo, OutputSummary

LINEAR_PCM = 0x6C70636D
FLOAT_PACKED = 1 | 8
CF_STRING_ENCODING_UTF8 = 0x08000100
# Rust の OUTPUT_CALLBACK_WAIT。これだけコールバックが進まなければ止まったとみなす。
CALLBACK_WAIT_SECONDS = 3.0
# バッファが溜まる一方で一度も完了しないときの上限。先行分0.25秒を足す。
NO_CALLBACK_SECONDS = CALLBACK_WAIT_SECONDS + 0.25
NAME_LIMIT = 256


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
# 解決済みのパスごとに CDLL と署名を一度だけ用意する。find_library を差し替えた
# テストでは解決関数が変わるので、そのときだけ引き直す。
_library_paths: dict[str, tuple[object, str | None]] = {}
_libraries: dict[str, object] = {}


def _find(name: str):
    finder = ctypes.util.find_library
    cached = _library_paths.get(name)
    if cached is not None and cached[0] is finder:
        return cached[1]
    path = finder(name)
    _library_paths[name] = (finder, path)
    return path


def _open_library(path: str):
    loaded = _libraries.get(path)
    if loaded is None:
        loaded = ctypes.CDLL(path)
        _libraries[path] = loaded
    return loaded


def library():
    """darwin で AudioToolbox が見つかったときだけ返す。"""
    if sys.platform != "darwin":
        return None
    path = _find("AudioToolbox")
    if not path:
        return None
    loaded = _open_library(path)
    _bind(loaded)
    return loaded


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


def sanitize_device_name(text: str) -> str:
    """Rust 同様、制御文字を落とし256文字で切る。"""
    return "".join(char for char in text if unicodedata.category(char) != "Cc")[:NAME_LIMIT]


def inspect(rate: int, opened=None) -> OutputInfo:
    device = _default_output()
    if device is None:
        raise OutputError("none")
    name, _nominal, formats = device
    name = sanitize_device_name(name)
    channels = 2 if _supports(rate, 2, formats) else 1 if _supports(rate, 1, formats) else None
    if channels is not None:
        return OutputInfo(name, rate, "f32", channels, "")
    mismatch = _mismatch(rate, formats)
    return OutputInfo(name, rate, None, None, mismatch)


class Stream:
    """1つの AudioQueue を複数チャンクで使い回す。"""

    def __init__(self, rate: int, channels: int, stop, step_hook=None) -> None:
        self.rate = rate
        self.channels = channels
        self.stop = stop or threading.Event()
        self.step_hook = step_hook
        self.outstanding = 0
        self.lib = library()
        if self.lib is None:
            raise OutputError("none")

        def completed(_user, queue, buffer) -> None:
            self.outstanding = max(0, self.outstanding - 1)
            self.last_callback = time.monotonic()
            frames = 0
            if buffer:
                # CFUNCTYPE は void* を int で渡す。バッファの mUserData に積んだフレーム数を読む。
                holder = ctypes.cast(buffer, ctypes.POINTER(QueueBuffer)).contents
                frames = int(holder.mUserData or 0)
            self.completed_frames += frames
            free = getattr(self.lib, "AudioQueueFreeBuffer", None)
            if free is not None and buffer:
                free(queue, buffer)

        self.callback = _OUTPUT_CALLBACK(completed)
        self.handle = _open(rate, channels, lib=self.lib, callback=self.callback)
        if self.handle is None:
            raise OutputError("none")
        self.lib, self.queue, self.callback = self.handle
        self.started = False
        self.started_at = 0.0
        self.queued_frames = 0
        self.played_frames = 0
        self.completed_frames = 0
        self.last_callback: float | None = None
        # 未完了バッファが 0→1 になった時刻。休止あけの猶予をここから数える。
        self.queue_active_at = 0.0
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
        # 1秒以内に区切る。1本の長いバッファだと完了通知が来ず、停滞と区別できない。
        chunk = max(int(self.rate), 1)
        cursor = 0
        while cursor < frames:
            if self.stop.is_set():
                return
            step = min(chunk, frames - cursor)
            piece = payload[cursor * self.channels : (cursor + step) * self.channels]
            self._enqueue(piece, step)
            cursor += step
            if not self.started:
                status = self.lib.AudioQueueStart(self.queue, None)
                if status != 0:
                    raise OutputError("device")
                self.started = True
                self.started_at = time.monotonic()
            self.queued_frames += step
            self._wait(self.started_at + self.queued_frames / self.rate - 0.25)

    def _enqueue(self, piece, frames: int) -> None:
        size = piece.nbytes
        buffer_ref = ctypes.POINTER(QueueBuffer)()
        status = self.lib.AudioQueueAllocateBuffer(
            self.queue, max(size, 4), ctypes.byref(buffer_ref)
        )
        if status != 0 or not buffer_ref:
            raise OutputError("device")
        destination = ctypes.cast(buffer_ref.contents.mAudioData, ctypes.POINTER(ctypes.c_char))
        ctypes.memmove(destination, piece.ctypes.data, size)
        buffer_ref.contents.mAudioDataByteSize = size
        # 完了コールバックはこの値で再生済みフレームを数える。
        buffer_ref.contents.mUserData = frames
        if self.outstanding == 0:
            self.queue_active_at = time.monotonic()
        self.outstanding += 1
        status = self.lib.AudioQueueEnqueueBuffer(self.queue, buffer_ref, 0, None)
        if status != 0:
            self.outstanding -= 1
            # キューに渡せなかったバッファは完了通知が来ないので、ここで返す。
            free = getattr(self.lib, "AudioQueueFreeBuffer", None)
            if free is not None:
                free(self.queue, buffer_ref)
            raise OutputError("device")

    def _wait(self, deadline: float) -> None:
        while True:
            if self.step_hook is not None:
                self.step_hook(self.queued_frames)
            if self.stop.is_set():
                return
            self._check_stalled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.01, remaining))

    def _check_stalled(self) -> None:
        """未完了バッファがあるのに進まないときだけ止まったとみなす。"""
        if not self.started or self.stop.is_set() or self.outstanding <= 0:
            return
        anchor = max(self.last_callback or 0.0, self.queue_active_at)
        limit = CALLBACK_WAIT_SECONDS if self.last_callback is not None else NO_CALLBACK_SECONDS
        if time.monotonic() - anchor > limit:
            raise OutputError("stalled")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.started:
                if not self.stop.is_set():
                    try:
                        self._wait(self.started_at + self.queued_frames / self.rate)
                    except OutputError:
                        pass  # 停止・停滞で閉じる。呼び出し元のエラーを優先する。
                elapsed = time.monotonic() - self.started_at
                if self.stop.is_set():
                    self.played_frames = min(self.queued_frames, max(0, int(elapsed * self.rate)))
                elif self.last_callback is not None:
                    self.played_frames = min(self.queued_frames, self.completed_frames)
                else:
                    self.played_frames = self.queued_frames
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
        try:
            stream.close()
        except OutputError:
            pass  # 元のエラーを優先する。
        raise
    return OutputSummary(stream.played_frames, rate, stream.channels, "f32", 0)


def _open(rate: int, channels: int, lib=None, callback=None):
    lib = library() if lib is None else lib
    if lib is None:
        return None
    _bind(lib)
    description = _description(rate, channels)
    queue = ctypes.c_void_p()
    if callback is None:
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
    path = _find(name)
    if not path:
        return None
    return _open_library(path)


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
        buffer = ctypes.create_string_buffer(NAME_LIMIT * 4 + 1)
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
) -> list[tuple[float, float, int, str]] | None:
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
                label = _format_label(fmt)
                if label is not None and fmt.mChannelsPerFrame >= 1:
                    formats.append(
                        (
                            item.mSampleRateRange.mMinimum,
                            item.mSampleRateRange.mMaximum,
                            int(fmt.mChannelsPerFrame),
                            label,
                        )
                    )
            continue
        if _float_physical_format(coreaudio, stream):
            for rate in nominal:
                formats.extend([(rate, rate, 1, "f32"), (rate, rate, 2, "f32")])
    return formats


def _format_label(fmt: StreamDescription) -> str | None:
    """linear PCM のビットレイアウトから Rust と同じラベルを作る。"""
    if fmt.mFormatID != LINEAR_PCM:
        return None
    bits = int(fmt.mBitsPerChannel)
    flags = int(fmt.mFormatFlags)
    if flags & 1:
        return {32: "f32", 64: "f64"}.get(bits, "その他")
    # kAudioFormatFlagIsSignedInteger は 1<<2。1<<1 は big-endian。
    if flags & 4:
        return {8: "i8", 16: "i16", 32: "i32", 64: "i64"}.get(bits, "その他")
    return {8: "u8", 16: "u16", 32: "u32", 64: "u64"}.get(bits, "その他")


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
    """再生に使えるのは f32 のみ。旧来の3要素タプルは f32 として扱う。"""
    for item in formats:
        if len(item) >= 4 and item[3] != "f32":
            continue
        if item[2] == channels and item[0] <= rate <= item[1]:
            return True
    return False


def _mismatch(rate: int, formats) -> str:
    """非対応時に、機器が宣言した全フォーマットを Rust と同じ形で列挙する。"""
    entries: list[str] = []
    for item in formats:
        minimum, maximum, channels = item[0], item[1], item[2]
        if channels < 1:
            continue
        label = item[3] if len(item) >= 4 else "f32"
        span = f"{minimum:g}Hz" if minimum == maximum else f"{minimum:g}〜{maximum:g}Hz"
        text = f"{span}・{label}・{channels}ch"
        if text not in entries:
            entries.append(text)
    ranges = "、".join(entries[:12]) if entries else "なし"
    if len(entries) > 12:
        ranges += "、ほか"
    return f"必要な形式は{rate}Hzのmonoまたはstereoのf32・i16・i32です。機器の対応範囲: {ranges}"


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
    if getattr(lib, "_radiko_signed", False):
        return
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
        "AudioQueueFreeBuffer": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int32),
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
    try:
        lib._radiko_signed = True
    except (AttributeError, TypeError):
        pass  # 署名を記録できない相手でも、毎回設定し直せば同じ結果になる。


def _signature(function, argtypes, restype) -> None:
    if hasattr(function, "argtypes"):
        function.argtypes = argtypes
        function.restype = restype
