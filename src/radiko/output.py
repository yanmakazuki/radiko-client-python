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
from array import array
from contextlib import contextmanager
from dataclasses import dataclass

PCM_RATE_MIN = 8000
PCM_RATE_MAX = 192000


class OutputError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message
        super().__init__(message or _default(code))

    def __str__(self) -> str:
        return self.message or _default(self.code)


def _default(code: str) -> str:
    if code == "none":
        return "既定の音声出力機器がありません"
    if code == "stalled":
        return "出力が停止しました"
    if code == "device":
        return "出力機器を確認してください"
    return "出力機器を確認してください"


@dataclass
class OutputInfo:
    name: str
    sample_rate: int
    sample_format: str | None
    channels: int | None
    mismatch: str


@dataclass
class OutputSummary:
    frames: int
    sample_rate: int
    channels: int
    sample_format: str
    underrun_frames: int


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


def inspect_default_output(rate: int) -> OutputInfo:
    if not PCM_RATE_MIN <= rate <= PCM_RATE_MAX:
        raise OutputError("device", "サンプルレートは8000〜192000Hzで指定してください")
    if null_sink_enabled():
        return OutputInfo("null", rate, "f32", 2, "")
    if host_backend() == "coreaudio":
        from radiko.coreaudio import inspect as inspect_coreaudio

        return inspect_coreaudio(rate)
    opened = _open_default()
    if opened is None:
        raise OutputError("none")
    chosen = _select_format(opened, rate)
    _close(opened)
    if chosen is None:
        mismatch = (
            f"必要な形式は{rate}Hzのmonoまたはstereoのf32・i16・i32です。機器の対応範囲: なし"
        )
        return OutputInfo("default", rate, None, None, mismatch)
    sample_format, channels = chosen
    return OutputInfo("default", rate, sample_format, channels, "")


def probe_tone(seconds: int) -> OutputSummary:
    if not 1 <= seconds <= 10:
        raise OutputError("pcm", "出力用PCMまたは再生時間が不正です")
    return _play_generated(seconds, tone=True)


def probe_silent(seconds: int, stop, on_progress=None) -> OutputSummary:
    if not 1 <= seconds <= 1800:
        raise OutputError("device", "無音の出力診断は1〜1800秒で指定してください")
    return _play_generated(seconds, tone=False, stop=stop, on_progress=on_progress)


def play_samples(
    samples: list[float],
    rate: int,
    channels: int,
    stop=None,
    paused=None,
    volume: int = 100,
    step_hook=None,
) -> OutputSummary:
    """インターリーブされた f32 を出す。一時停止中はメディア位置を進めない。"""
    stop = stop or threading.Event()
    paused = paused or threading.Event()
    info = inspect_default_output(rate)
    if info.sample_format is None or info.channels is None:
        raise OutputError("device", info.mismatch)
    gain = max(0, min(volume, 100)) / 100
    scaled = [sample * gain for sample in samples]
    return _consume(scaled, rate, channels, info, stop, paused, step_hook)


def _play_generated(seconds: int, tone: bool, stop=None, on_progress=None) -> OutputSummary:
    rate = 48000
    channels = 2
    total = rate * seconds
    samples: list[float] = []
    if tone:
        for index in range(total):
            value = 0.2 if (index // (rate // 440)) % 2 == 0 else -0.2
            samples.extend([value, value])
    else:
        samples = [0.0] * (total * channels)
    summary = play_samples(samples, rate, channels, stop)
    if on_progress:
        on_progress(summary)
    return summary


def _consume(
    samples, rate, channels, info: OutputInfo, stop, paused, step_hook=None
) -> OutputSummary:
    frames = len(samples) // channels
    underruns = 0
    played = 0
    quantum = max(rate // 50, 1)
    cursor = 0
    if null_sink_enabled():
        while cursor < frames:
            if step_hook is not None:
                step_hook(played)
            if stop.is_set():
                break
            if paused.is_set():
                break
            step = min(quantum, frames - cursor)
            cursor += step
            played += step
        return OutputSummary(
            played, rate, info.channels or channels, info.sample_format or "f32", underruns
        )
    if host_backend() == "coreaudio":
        from radiko.coreaudio import play as play_coreaudio

        return play_coreaudio(samples, rate, channels, info, stop)
    opened = _open_default()
    if opened is None:
        raise OutputError("none")
    try:
        chosen = _select_format(opened, rate)
        if chosen is None:
            raise OutputError("device", inspect_default_output(rate).mismatch)
        sample_format, out_channels = chosen
        while cursor < frames:
            if stop.is_set():
                break
            step = min(quantum, frames - cursor)
            if paused.is_set():
                _write_silence(opened, sample_format, out_channels, step)
                continue
            block = samples[cursor * channels : (cursor + step) * channels]
            if out_channels != channels:
                block = _remix(block, channels, out_channels, step)
            _write_pcm(opened, block, sample_format, out_channels)
            cursor += step
            played += step
    finally:
        _close(opened)
    return OutputSummary(
        played, rate, info.channels or channels, info.sample_format or "f32", underruns
    )


def _remix(block: list[float], src: int, dst: int, frames: int) -> list[float]:
    output = []
    for index in range(frames):
        frame = block[index * src : (index + 1) * src]
        if dst == 1:
            output.append(sum(frame) / len(frame))
        elif src == 1:
            output.extend([frame[0], frame[0]])
        else:
            output.extend(frame[:dst])
    return output


def _alsa():
    path = ctypes.util.find_library("asound")
    if not path:
        return None
    library = ctypes.CDLL(path)
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
    return library


_FORMATS = (("f32", 14), ("i32", 10), ("i16", 2))


def _open_default():
    library = _alsa()
    if library is None:
        return None
    handle = ctypes.c_void_p()
    with _silence_stderr():
        code = library.snd_pcm_open(ctypes.byref(handle), b"default", 0, 0)
    if code < 0 or not handle.value:
        return None
    return library, handle


def _close(opened) -> None:
    library, handle = opened
    library.snd_pcm_close(handle)


def _select_format(opened, rate: int):
    library, handle = opened
    for channels in (2, 1):
        for name, code in _FORMATS:
            with _silence_stderr():
                result = library.snd_pcm_set_params(handle, code, 3, channels, rate, 0, 100000)
            if result >= 0:
                return name, channels
    return None


def _write_pcm(opened, samples: list[float], sample_format: str, channels: int) -> None:
    library, handle = opened
    if sample_format == "f32":
        raw = array("f", samples)
    elif sample_format == "i16":
        raw = array("h", [max(-32768, min(32767, int(sample * 32767))) for sample in samples])
    else:
        raw = array(
            "i", [max(-2147483648, min(2147483647, int(sample * 2147483647))) for sample in samples]
        )
    frames = len(samples) // channels
    written = library.snd_pcm_writei(handle, raw.tobytes(), frames)
    if written < 0:
        raise OutputError("device")


def _write_silence(opened, sample_format: str, channels: int, frames: int) -> None:
    _write_pcm(opened, [0.0] * (frames * channels), sample_format, channels)


@contextmanager
def _silence_stderr():
    if show_alsa_logs():
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
