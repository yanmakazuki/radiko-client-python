"""音声出力。Linux は libasound の default。試験はプロセス内 null sink。

`RADIKO_NULL_SINK=1` は、Rust 版 CI の PulseAudio null sink に相当する
プロセス内の出力である。ハードウェアクロックは進めず、渡したフレーム数だけ進む。
`RADIKO_ALSA_LOG=1` のときだけ ALSA の診断を標準エラーに残す。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import cast

import numpy as np

from radiko import coreaudio
from radiko.audio import OutputCapability, OutputError, OutputInfo, OutputSummary
from radiko.dynamic_library import find_library, open_library

PCM_RATE_MIN = 8000
PCM_RATE_MAX = 192000
# ALSA の負の戻り値。バッファ枯渇と、ノンブロックの書き込み待ち。
EPIPE = -32
EAGAIN = -11
# EAGAIN / 0件書き込みの打ち切り。1回1msで最大0.1秒。
MAX_WRITE_RETRIES = 100


def null_sink_enabled() -> bool:
    """`RADIKO_NULL_SINK=1` ならプロセス内の null sink を使う。"""
    return os.environ.get("RADIKO_NULL_SINK") == "1"


def host_backend() -> str:
    """実行中の OS が使う出力。Linux は ALSA、macOS は CoreAudio。"""
    if sys.platform == "darwin":
        return "coreaudio"
    if sys.platform.startswith("linux"):
        return "alsa"
    return "unsupported"


def show_alsa_logs() -> bool:
    """`RADIKO_ALSA_LOG=1` なら ALSA の診断を標準エラーに残す。"""
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
    """指定レート・チャンネルの f32 を ALSA で設定できるか。機器が無ければ None。"""
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
    """機器の対応範囲が無いときの不一致文を返す。"""
    return f"必要な形式は{rate}Hzのmonoまたはstereoのf32・i16・i32です。機器の対応範囲: なし"


def inspect_default_output(rate: int) -> OutputInfo:
    """既定出力機器の対応形式を調べる。

    null sink は f32 stereo。レート範囲外は OutputError("device")、機器なしは
    "none"、形式が合わなければ sample_format / channels を None にして理由を返す。
    """
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
        """出力レート・入力/出力チャンネル・サンプル形式とカウンタを初期化する。"""
        self.rate = rate
        self.channels = out_channels
        self.input_channels = channels
        self.sample_format = sample_format
        self.frames = 0
        self.underruns = 0

    def write(self, samples, volume: int = 100) -> None:
        """音声を書く。サブクラスが実装する。"""
        raise NotImplementedError

    def close(self) -> None:
        """出力を閉じる。既定では何もしない。"""
        pass

    def summary(self) -> OutputSummary:
        """出力済みフレームとアンダーランを OutputSummary にまとめる。"""
        return OutputSummary(
            self.frames, self.rate, self.channels, self.sample_format, self.underruns
        )

    def __enter__(self) -> OutputStream:
        """with の本体へ自分を返す。"""
        return self

    def __exit__(self, *_args) -> None:
        """with を抜けるときに close する。"""
        self.close()


class _NullStream(OutputStream):
    """プロセス内の null sink。実時間を待たず、渡したフレーム数だけ進む。"""

    def __init__(self, rate, channels, stop, paused, step_hook) -> None:
        """レート・チャンネル・停止/一時停止/ステップフックを保持する。"""
        super().__init__(rate, channels, "f32", channels)
        self.stop = stop or threading.Event()
        self.paused = paused or threading.Event()
        self.step_hook = step_hook

    def write(self, samples, volume: int = 100) -> None:
        """フレーム数を量子（rate/50）ずつ進める。停止・一時停止なら打ち切る。"""
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
    """ALSA の default を開き、選択した形式で書くストリーム。"""

    def __init__(self, rate, channels, stop, paused, step_hook) -> None:
        """default を開いて f32/i32/i16 のいずれかを選ぶ。機器なしは "none"、
        形式が合わなければ "device"。
        """
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
        """量子ずつ書き、一時停止中は無音を書く。入出力のチャンネル数が違えばリミックスし、
        部分書き込みと -EPIPE は _write_pcm が処理する。
        """
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
        """一度だけ ALSA のハンドルを閉じる。"""
        if not self.closed:
            self.closed = True
            _close(self.opened)


class _CoreAudioStream(OutputStream):
    """CoreAudio の Stream を OutputStream として包む。"""

    def __init__(self, rate, channels, stop, step_hook) -> None:
        """AudioQueue の Stream を作る。レート・チャンネル非対応なら OutputError("device")。"""
        info = coreaudio.inspect(rate)
        if info.sample_format is None or info.channels is None:
            raise OutputError("device", info.mismatch)
        self.stream = coreaudio.Stream(rate, info.channels, stop, step_hook)
        super().__init__(rate, channels, "f32", info.channels)

    def write(self, samples, volume: int = 100) -> None:
        """入力を出力チャンネル数にリミックスし、Stream へ書く。"""
        array = np.asarray(samples, dtype=np.float32).reshape(-1)
        frames = array.size // self.input_channels if self.input_channels else 0
        if self.input_channels and self.input_channels != self.channels:
            array = _remix(array, self.input_channels, self.channels, frames)
        self.stream.write(array, volume)
        self.frames = self.stream.queued_frames

    def close(self) -> None:
        """Stream を閉じ、再生済みフレームを確定する。"""
        self.stream.close()
        self.frames = self.stream.played_frames
        self.underruns = 0


def open_stream(rate: int, channels: int, stop=None, paused=None, step_hook=None) -> OutputStream:
    """バックエンドを選んでストリームを開く。

    null sink → CoreAudio → ALSA の順。未対応 OS は OutputError("none")。
    """
    if null_sink_enabled():
        return _NullStream(rate, channels, stop, paused, step_hook)
    backend = host_backend()
    if backend == "coreaudio":
        return _CoreAudioStream(rate, channels, stop, step_hook)
    if backend == "alsa":
        return _AlsaStream(rate, channels, stop, paused, step_hook)
    raise OutputError("none")


def probe_tone(seconds: int) -> OutputSummary:
    """440Hz・振幅0.2の生成音を1〜10秒出す。秒数が不正なら OutputError("pcm")。"""
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
    """無音を1〜1800秒出す。5秒ごとに on_progress を呼び、停止で打ち切る。
    秒数が不正なら OutputError("device")。
    """
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
    """入力を f32 にそろえ、音量 0〜100% を掛ける。"""
    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    gain = max(0, min(volume, 100)) / 100
    return array if gain == 1.0 else array * gain


def _remix(block, src: int, dst: int, frames: int) -> np.ndarray:
    """チャンネル数を変換する。mono は平均、stereo 化は複製、それ以外は先頭 dst 本を使う。"""
    array = np.asarray(block, dtype=np.float32).reshape(frames, src)
    if dst == 1:
        output = array.mean(axis=1, dtype=np.float32)
    elif src == 1:
        output = np.repeat(array, 2, axis=1)
    else:
        output = array[:, :dst]
    return np.ascontiguousarray(output.reshape(-1), dtype=np.float32)


def _alsa():
    """libasound を読み、ctypes 署名を付けて返す。見つからなければ None。"""
    path = find_library("asound")
    if not path:
        return None
    library = open_library(path)
    _bind_alsa(library)
    return library


def _bind_alsa(library) -> None:
    """ALSA の関数に ctypes の argtypes/restype を設定する。設定済みなら何もしない。"""
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
    """ALSA の default を開き、(library, handle) を返す。失敗は None。"""
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
    """ALSA の PCM ハンドルを閉じる。"""
    library, handle = opened
    library.snd_pcm_close(handle)


def _select_format(opened, rate: int):
    """f32 → i32 → i16、各2ch → 1ch の順に設定を試し、最初に通った (形式, ch) を返す。
    全滅なら None。
    """
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
    base_address = cast(int, base.value)
    while cursor < frames:
        pointer = ctypes.c_void_p(base_address + cursor * item_size)
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
    """指定フレーム分の無音を _write_pcm で書く。"""
    return _write_pcm(opened, _silence(frames * channels), sample_format, channels)


_ALSA_ERROR = ctypes.CFUNCTYPE(
    None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p
)


def _alsa_noop(*_args) -> None:
    """ALSA のエラーハンドラ用の何もしない関数。"""
    return


_ALSA_ERROR_HANDLER = _ALSA_ERROR(_alsa_noop)


@contextmanager
def _silence_alsa(library):
    """ALSA のエラー出力を抑える context manager。`RADIKO_ALSA_LOG=1` ならそのまま流す。"""
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
