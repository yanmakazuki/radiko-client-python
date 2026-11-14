"""OS の共有ライブラリを解決し、パスごとに1つだけ読み込む。"""

from __future__ import annotations

import ctypes
import ctypes.util

# 解決済みのパスごとに CDLL と署名を一度だけ用意する。find_library を差し替えた
# テストでは解決関数が変わるので、そのときだけ引き直す。
_library_paths: dict[str, tuple[object, str | None]] = {}
_libraries: dict[str, object] = {}


def find_library(name: str) -> str | None:
    """`ctypes.util.find_library` の結果を名前ごとにキャッシュして返す。

    解決関数が差し替えられたらキャッシュを捨て、その関数で引き直す。
    """
    finder = ctypes.util.find_library
    cached = _library_paths.get(name)
    if cached is not None and cached[0] is finder:
        return cached[1]
    path = finder(name)
    _library_paths[name] = (finder, path)
    return path


def open_library(path: str) -> object:
    """パスごとに CDLL を1つだけ読み込み、以後は同じものを返す。"""
    loaded = _libraries.get(path)
    if loaded is None:
        loaded = ctypes.CDLL(path)
        _libraries[path] = loaded
    return loaded
