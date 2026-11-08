"""CLI の標準出力と標準エラー。閉じたパイプは終了コード 141。"""

from __future__ import annotations

import sys

BROKEN_PIPE_STATUS = 141


class ClosedPipe(Exception):
    def __str__(self) -> str:
        return "Broken pipe"


class WriteFailed(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(self.text)

    @property
    def text(self) -> str:
        if self.kind == "StorageFull":
            cause = "空き容量不足"
        elif self.kind == "PermissionDenied":
            cause = "書き込みが拒否されました"
        else:
            cause = self.kind
        return f"書き込みに失敗しました（{cause}）"


def stdout_ln(text: str = "") -> None:
    try:
        sys.stdout.write(f"{text}\n")
        sys.stdout.flush()
    except BrokenPipeError as error:
        raise ClosedPipe() from error
    except OSError as error:
        kind = "StorageFull" if getattr(error, "errno", None) == 28 else error.__class__.__name__
        raise WriteFailed(kind) from error


def stderr_ln(text: str = "") -> None:
    try:
        sys.stderr.write(f"{text}\n")
        sys.stderr.flush()
    except OSError:
        return


def stderr_write(text: str) -> None:
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except OSError:
        return
