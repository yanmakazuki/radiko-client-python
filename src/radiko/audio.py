"""音声出力の共通型。バックエンド（ALSA / CoreAudio / null）から独立させる。"""

from __future__ import annotations

from dataclasses import dataclass


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
class OutputCapability:
    host: str
    name: str
    sample_rate: int
    f32_mono: bool
    f32_stereo: bool


@dataclass
class OutputSummary:
    frames: int
    sample_rate: int
    channels: int
    sample_format: str
    underrun_frames: int
