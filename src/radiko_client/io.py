"""CLI の標準出力と標準エラー。閉じたパイプは終了コード 141。"""

from __future__ import annotations

import sys

BROKEN_PIPE_STATUS = 141


class ClosedPipe(Exception):
    """標準出力のパイプが閉じられたことを表す。終了コード141に対応する。"""

    def __str__(self) -> str:
        """固定の "Broken pipe" を返す。"""
        return "Broken pipe"


class WriteFailed(Exception):
    """標準出力への書き込み失敗。kind は StorageFull / PermissionDenied など。"""

    def __init__(self, kind: str) -> None:
        """失敗の種類を覚え、利用者向けメッセージを作る。"""
        self.kind = kind
        super().__init__(self.text)

    @property
    def text(self) -> str:
        """書き込み失敗の日本語メッセージ。StorageFull と PermissionDenied は原因を言い換える。"""
        if self.kind == "StorageFull":
            cause = "空き容量不足"
        elif self.kind == "PermissionDenied":
            cause = "書き込みが拒否されました"
        else:
            cause = self.kind
        return f"書き込みに失敗しました（{cause}）"


def stdout_ln(text: str = "") -> None:
    """1行書いて flush する。BrokenPipeError は ClosedPipe、その他の OSError は WriteFailed にする。"""
    try:
        sys.stdout.write(f"{text}\n")
        sys.stdout.flush()
    except BrokenPipeError as error:
        raise ClosedPipe() from error
    except OSError as error:
        kind = "StorageFull" if getattr(error, "errno", None) == 28 else error.__class__.__name__
        raise WriteFailed(kind) from error


def stderr_ln(text: str = "") -> None:
    """stderr に1行書いて flush する。OSError は無視する。"""
    try:
        sys.stderr.write(f"{text}\n")
        sys.stderr.flush()
    except OSError:
        return
