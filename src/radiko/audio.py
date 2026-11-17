"""音声出力の共通型。バックエンド（ALSA / CoreAudio / null）から独立させる。"""

from __future__ import annotations

from dataclasses import dataclass


class OutputError(Exception):
    """音声出力の失敗。code は none / stalled / device など、message は利用者向けの上書き。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """失敗コードと任意のメッセージを覚え、メッセージが無ければ既定文言を使う。"""
        self.code = code
        self.message = message
        super().__init__(message or _default(code))

    def __str__(self) -> str:
        """上書きメッセージか code の既定文言を返す。"""
        return self.message or _default(self.code)


def _default(code: str) -> str:
    """code ごとの既定文言。未知の code は機器確認を促す。"""
    if code == "none":
        return "既定の音声出力機器がありません"
    if code == "stalled":
        return "出力が停止しました"
    if code == "device":
        return "出力機器を確認してください"
    return "出力機器を確認してください"


@dataclass
class OutputInfo:
    """実際に開いた出力の情報。name・rate・sample_format・channels と、要求との不一致理由 mismatch。"""

    name: str
    sample_rate: int
    sample_format: str | None
    channels: int | None
    mismatch: str


@dataclass
class OutputCapability:
    """バックエンドが申告する能力。host/name/rate と f32 の mono/stereo 対応。"""

    host: str
    name: str
    sample_rate: int
    f32_mono: bool
    f32_stereo: bool


@dataclass
class OutputSummary:
    """出力ストリームの累計。frames・rate・channels・format・underrun。"""

    frames: int
    sample_rate: int
    channels: int
    sample_format: str
    underrun_frames: int
