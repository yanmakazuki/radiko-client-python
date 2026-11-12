"""音声出力。Linux は libasound の default。試験はプロセス内 null sink。

`RADIKO_NULL_SINK=1` は、Rust 版 CI の PulseAudio null sink に相当する
プロセス内の出力である。ハードウェアクロックは進めず、渡したフレーム数だけ進む。
`RADIKO_ALSA_LOG=1` のときだけ ALSA の診断を標準エラーに残す。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading
import time
from contextlib import contextmanager

import numpy as np

from radiko import coreaudio
from radiko.audio import OutputCapability, OutputError, OutputInfo, OutputSummary

PCM_RATE_MIN = 8000
PCM_RATE_MAX = 192000
# ALSA の負の戻り値。バッファ枯渇と、ノンブロックの書き込み待ち。
EPIPE = -32
EAGAIN = -11
# EAGAIN / 0件書き込みの打ち切り。1回1msで最大0.1秒。
MAX_WRITE_RETRIES = 100


def null_sink_enabled() -> bool:
    return os.environ.get("RADIKO_NULL_SINK") == "1"


def host_backend() -> str:
    """実行中の OS が使う出力。Linux は ALSA、macOS は CoreAudio。"""
    if sys.platform == "darwin":
        return "coreaudio"
    if sys.platform.startswith("linux"):
        return "alsa"
    return "unsupported"


def show_alsa_logs() -> bool:
    return os.environ.get("RADIKO_ALSA_LOG") == "1"


def diagnose_default_output(rate: int) -> OutputCapability:
    """既定機器の f32 mono / stereo を読む。再生ストリームは残さない。"""
    if not PCM_RATE_MIN <= rate <= PCM_RATE_MAX:
        raise OutputError("device", "サンプルレートは8000〜192000Hzで指定してください")
    if null_sink_enabled():
        return OutputCapability("null", "null", rate, True, True)
    if host_backend() == "coreaudio":
        info = coreaudio.inspect(rate)
        return OutputCapability(
            "CoreAudio",
            info.name,
            rate,
            info.channels == 1,
            info.channels == 2,
        )
    mono = _supports_f32(rate, 1)
    stereo = _supports_f32(rate, 2)
    if mono is None and stereo is None:
        raise OutputError("none")
    return OutputCapability("alsa", "default", rate, bool(mono), bool(stereo))


def _supports_f32(rate: int, channels: int) -> bool | None:
    opened = _open_default()
    if opened is None:
        return None
    library, handle = opened
    try:
        with _silence_alsa(library):
            result = library.snd_pcm_set_params(handle, 14, 3, channels, rate, 0, 100000)
        return result >= 0
    finally:
        _close(opened)


def _mismatch_none(rate: int) -> str:
    return f"必要な形式は{rate}Hzのmonoまたはstereoのf32・i16・i32です。機器の対応範囲: なし"


def inspect_default_output(rate: int) -> OutputInfo:
    if not PCM_RATE_MIN <= rate <= PCM_RATE_MAX:
        raise OutputError("device", "サンプルレートは8000〜192000Hzで指定してください")
    if null_sink_enabled():
        return OutputInfo("null", rate, "f32", 2, "")
    if host_backend() == "coreaudio":
        return coreaudio.inspect(rate)
    opened = _open_default()
    if opened is None:
        raise OutputError("none")
    chosen = _select_format(opened, rate)
    _close(opened)
    if chosen is None:
        return OutputInfo("default", rate, None, None, _mismatch_none(rate))
    sample_format, channels = chosen
    return OutputInfo("default", rate, sample_format, channels, "")


class OutputStream:
    """1回開いた出力を複数のチャンクで使い回す。"""

    def __init__(self, rate, channels, sample_format, out_channels) -> None:
        self.rate = rate
        self.channels = out_channels
        self.input_channels = channels
        self.sample_format = sample_format
        self.frames = 0
        self.underruns = 0

    def write(self, samples, volume: int = 100) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def summary(self) -> OutputSummary:
        return OutputSummary(
            self.frames, self.rate, self.channels, self.sample_format, self.underruns
        )

    def __enter__(self) -> OutputStream:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class _NullStream(OutputStream):
    def __init__(self, rate, channels, stop, paused, step_hook) -> None:
        super().__init__(rate, channels, "f32", channels)
        self.stop = stop or threading.Event()
        self.paused = paused or threading.Event()
        self.step_hook = step_hook

    def write(self, samples, volume: int = 100) -> None:
        array = _prepare(samples, volume)
        frames = array.size // self.input_channels
        quantum = max(self.rate // 50, 1)
        cursor = 0
        while cursor < frames:
            if self.step_hook is not None:
                self.step_hook(self.frames)
            if self.stop.is_set() or self.paused.is_set():
                break
            step = min(quantum, frames - cursor)
            cursor += step
            self.frames += step


class _AlsaStream(OutputStream):
    def __init__(self, rate, channels, stop, paused, step_hook) -> None:
        opened = _open_default()
        if opened is None:
            raise OutputError("none")
        self.opened = opened
        chosen = _select_format(opened, rate)
        if chosen is None:
            _close(opened)
            raise OutputError("device", _mismatch_none(rate))
        sample_format, out_channels = chosen
        super().__init__(rate, channels, sample_format, out_channels)
        self.stop = stop or threading.Event()
        self.paused = paused or threading.Event()
        self.step_hook = step_hook
        self.quantum = max(rate // 50, 1)
        self.closed = False

    def write(self, samples, volume: int = 100) -> None:
        array = _prepare(samples, volume)
        frames = array.size // self.input_channels
        cursor = 0
        while cursor < frames:
            if self.step_hook is not None:
                self.step_hook(self.frames)
            if self.stop.is_set():
                break
            step = min(self.quantum, frames - cursor)
            if self.paused.is_set():
                _written, underruns = _write_silence(
                    self.opened, self.sample_format, self.channels, step
                )
                self.underruns += underruns
                continue
            block = array[cursor * self.input_channels : (cursor + step) * self.input_channels]
            if self.channels != self.input_channels:
                block = _remix(block, self.input_channels, self.channels, step)
            _written, underruns = _write_pcm(self.opened, block, self.sample_format, self.channels)
            self.underruns += underruns
            cursor += step
            self.frames += step

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            _close(self.opened)


class _CoreAudioStream(OutputStream):
    def __init__(self, rate, channels, stop, step_hook) -> None:
        info = coreaudio.inspect(rate)
        if info.sample_format is None or info.channels is None:
            raise OutputError("device", info.mismatch)
        self.stream = coreaudio.Stream(rate, info.channels, stop, step_hook)
        super().__init__(rate, channels, "f32", info.channels)

    def write(self, samples, volume: int = 100) -> None:
        array = np.asarray(samples, dtype=np.float32).reshape(-1)
        frames = array.size // self.input_channels if self.input_channels else 0
        if self.input_channels and self.input_channels != self.channels:
            array = _remix(array, self.input_channels, self.channels, frames)
        self.stream.write(array, volume)
        self.frames = self.stream.queued_frames

    def close(self) -> None:
        self.stream.close()
        self.frames = self.stream.played_frames
        self.underruns = 0


def open_stream(rate: int, channels: int, stop=None, paused=None, step_hook=None) -> OutputStream:
    if null_sink_enabled():
        return _NullStream(rate, channels, stop, paused, step_hook)
    backend = host_backend()
    if backend == "coreaudio":
        return _CoreAudioStream(rate, channels, stop, step_hook)
    if backend == "alsa":
        return _AlsaStream(rate, channels, stop, paused, step_hook)
    raise OutputError("none")


def probe_tone(seconds: int) -> OutputSummary:
    if not 1 <= seconds <= 10:
        raise OutputError("pcm", "出力用PCMまたは再生時間が不正です")
    rate = 48000
    channels = 2
    positions = np.arange(rate * seconds, dtype=np.float64)
    values = (0.2 * np.sin(2 * np.pi * 440 * positions / rate)).astype(np.float32)
    samples = np.repeat(values, channels)
    summary = play_samples(samples, rate, channels)
    return summary


def probe_silent(seconds: int, stop, on_progress=None) -> OutputSummary:
    if not 1 <= seconds <= 1800:
        raise OutputError("device", "無音の出力診断は1〜1800秒で指定してください")
    rate = 48000
    channels = 2
    silence = _silence(rate * channels)
    with open_stream(rate, channels, stop) as stream:
        for index in range(seconds):
            if stop.is_set():
                break
            stream.write(silence)
            if on_progress is not None and (index + 1) % 5 == 0:
                on_progress(stream.summary())
        stream.close()
        summary = stream.summary()
    return summary


def play_samples(
    samples: list[float] | np.ndarray,
    rate: int,
    channels: int,
    stop=None,
    paused=None,
    volume: int = 100,
    step_hook=None,
) -> OutputSummary:
    """インターリーブされた f32 を出す。一時停止中はメディア位置を進めない。"""
    with open_stream(rate, channels, stop, paused, step_hook) as stream:
        stream.write(samples, volume)
        stream.close()
        return stream.summary()


def _prepare(samples, volume: int) -> np.ndarray:
    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    gain = max(0, min(volume, 100)) / 100
    return array if gain == 1.0 else array * gain


def _remix(block, src: int, dst: int, frames: int) -> np.ndarray:
    array = np.asarray(block, dtype=np.float32).reshape(frames, src)
    if dst == 1:
        output = array.mean(axis=1, dtype=np.float32)
    elif src == 1:
        output = np.repeat(array, 2, axis=1)
    else:
        output = array[:, :dst]
    return np.ascontiguousarray(output.reshape(-1), dtype=np.float32)


def _alsa():
    path = _find("asound")
    if not path:
        return None
    library = _open_library(path)
    _bind_alsa(library)
    return library


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
    library = _libraries.get(path)
    if library is None:
        library = ctypes.CDLL(path)
        _libraries[path] = library
    return library


def _bind_alsa(library) -> None:
    if getattr(library, "_radiko_signed", False):
        return
    library.snd_pcm_open.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.snd_pcm_open.restype = ctypes.c_int
    library.snd_pcm_close.argtypes = [ctypes.c_void_p]
    library.snd_pcm_close.restype = ctypes.c_int
    library.snd_pcm_set_params.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.snd_pcm_set_params.restype = ctypes.c_int
    library.snd_pcm_writei.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    library.snd_pcm_writei.restype = ctypes.c_long
    library.snd_pcm_recover.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    library.snd_pcm_recover.restype = ctypes.c_int
    library.snd_pcm_prepare.argtypes = [ctypes.c_void_p]
    library.snd_pcm_prepare.restype = ctypes.c_int
    try:
        library._radiko_signed = True
    except (AttributeError, TypeError):
        pass  # 署名を記録できない相手でも、毎回設定し直せば同じ結果になる。


_FORMATS = (("f32", 14), ("i32", 10), ("i16", 2))


def _open_default():
    library = _alsa()
    if library is None:
        return None
    handle = ctypes.c_void_p()
    with _silence_alsa(library):
        code = library.snd_pcm_open(ctypes.byref(handle), b"default", 0, 0)
    if code < 0 or not handle.value:
        return None
    return library, handle


def _close(opened) -> None:
    library, handle = opened
    library.snd_pcm_close(handle)


def _select_format(opened, rate: int):
    library, handle = opened
    for name, code in _FORMATS:
        for channels in (2, 1):
            with _silence_alsa(library):
                result = library.snd_pcm_set_params(handle, code, 3, channels, rate, 0, 100000)
            if result >= 0:
                return name, channels
    return None


def _write_pcm(opened, samples, sample_format: str, channels: int) -> tuple[int, int]:
    """全部書き切るまでループする。（書き込んだフレーム, アンダーランフレーム)を返す。"""
    library, handle = opened
    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if sample_format == "f32":
        payload = np.ascontiguousarray(array, dtype=np.float32)
    elif sample_format == "i16":
        payload = np.clip(array * 32767, -32768, 32767).astype(np.int16)
    else:
        payload = np.clip(array * 2147483647, -2147483648, 2147483647).astype(np.int32)
    frames = array.size // channels
    item_size = payload.itemsize * channels
    base = payload.ctypes.data_as(ctypes.c_void_p)
    cursor = 0
    underruns = 0
    retries = 0
    while cursor < frames:
        pointer = ctypes.c_void_p(base.value + cursor * item_size)
        written = library.snd_pcm_writei(handle, pointer, frames - cursor)
        if written == 0:
            retries += 1
            if retries > MAX_WRITE_RETRIES:
                raise OutputError("device")
            time.sleep(0.001)
            continue
        if written < 0:
            code = int(written)
            if code == EPIPE:
                # バッファ枯渇。回復し、その時点で書き切れなかった分を不足として数える。
                underruns += frames - cursor
                _recover(library, handle, code)
                retries += 1
                if retries > MAX_WRITE_RETRIES:
                    raise OutputError("device")
                continue
            if code == EAGAIN:
                retries += 1
                if retries > MAX_WRITE_RETRIES:
                    raise OutputError("device")
                time.sleep(0.001)
                continue
            raise OutputError("device")
        retries = 0
        cursor += int(written)
    return frames, underruns


def _recover(library, handle, code: int) -> None:
    """XRUN の回復。snd_pcm_recover が無ければ snd_pcm_prepare で立て直す。"""
    recover = getattr(library, "snd_pcm_recover", None)
    with _silence_alsa(library):
        if recover is not None and recover(handle, code, 1) >= 0:
            return
        prepare = getattr(library, "snd_pcm_prepare", None)
        if prepare is None or prepare(handle) < 0:
            raise OutputError("device")


_SILENCE = np.zeros(0, dtype=np.float32)


def _silence(size: int) -> np.ndarray:
    """無音バッファをサイズ単位で使い回す。返り値は共有バッファのビュー。"""
    global _SILENCE
    if _SILENCE.size < size:
        _SILENCE = np.zeros(max(size, _SILENCE.size * 2), dtype=np.float32)
    return _SILENCE[:size]


def _write_silence(opened, sample_format: str, channels: int, frames: int) -> tuple[int, int]:
    return _write_pcm(opened, _silence(frames * channels), sample_format, channels)


_ALSA_ERROR = ctypes.CFUNCTYPE(
    None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p
)


def _alsa_noop(*_args) -> None:
    return


_ALSA_ERROR_HANDLER = _ALSA_ERROR(_alsa_noop)


@contextmanager
def _silence_alsa(library):
    if show_alsa_logs():
        yield
        return
    setter = getattr(library, "snd_lib_error_set_handler", None)
    if setter is not None:
        signature = getattr(setter, "argtypes", None)
        if signature is None or signature != [_ALSA_ERROR]:
            setter.argtypes = [_ALSA_ERROR]
            setter.restype = ctypes.c_int
        setter(_ALSA_ERROR_HANDLER)
        yield
        return
    saved = os.dup(2)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(null)
