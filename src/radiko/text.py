"""表示幅と書記素。unicode-width 0.2（曖昧幅は 1）と UAX #29 の拡張書記素に合わせる。"""

from __future__ import annotations

import functools
import re
import unicodedata

import regex

_GRAPHEME_RE = regex.compile(r"\X")

_ZERO_WIDTH = {
    "Mn",
    "Me",
    "Cf",
}


def _control_table() -> dict[int, str | None]:
    """Cc（U+0000〜U+001F, U+007F〜U+009F）だけを一括変換する表。"""
    table: dict[int, str | None] = {}
    for code in range(0x00, 0xA0):
        char = chr(code)
        if unicodedata.category(char) != "Cc":
            continue
        table[code] = " " if char.isspace() else None
    return table


_CONTROL_TABLE = _control_table()
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def graphemes(text: str) -> list[str]:
    """拡張書記素クラスタ。`regex` の `\\X` は UAX #29 である。"""
    if text == "":
        return []
    return _GRAPHEME_RE.findall(text)


def display_width(text: str) -> int:
    """制御文字は 0、全角と全幅は 2、それ以外の可視クラスタは 1。曖昧幅は 1。"""
    if text.isascii() and text.isprintable():
        return len(text)
    width = 0
    for cluster in graphemes(text):
        width += cluster_width(cluster)
    return width


@functools.lru_cache(maxsize=4096)
def cluster_width(cluster: str) -> int:
    """1クラスタの表示幅。同じクラスタはフレームを跨いで再利用する。"""
    if cluster == "":
        return 0
    if len(cluster) == 1:
        code = ord(cluster)
        if 0x20 <= code < 0x7F:
            return 1
    wide = False
    visible = False
    for char in cluster:
        if _is_zero(char):
            continue
        visible = True
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            wide = True
    if not visible:
        return 0
    return 2 if wide else 1


def _is_zero(char: str) -> bool:
    if unicodedata.category(char) in _ZERO_WIDTH:
        return True
    code = ord(char)
    if code < 32 or code == 0x7F:
        return True
    return unicodedata.combining(char) != 0


def clean_text(text: str) -> str:
    """空白は一つに畳み、Cc の制御文字は捨てる。"""
    return " ".join(text.translate(_CONTROL_TABLE).split())


def has_control(text: str) -> bool:
    return _CONTROL_PATTERN.search(text) is not None
