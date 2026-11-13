"""Issue #29: 一覧キャッシュと文字幅キャッシュの契約を固定する。"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta

import pytest

from radiko.domain import JST, Filter, Program, ProgramKey
from radiko.text import _is_zero, cluster_width, display_width, graphemes
from radiko_client.tui import LineEditor, Screen, _boundaries, truncate_width

SAMPLES = [
    "",
    "a",
    "abc",
    "a\x01b",
    "あ",
    "ｱ",
    "α",
    "Ａ",
    "e\u0301",
    "が",
    "\u2764\ufe0f",
    "👨‍👩‍👧‍👦",
    "🇯🇵",
    "\u0301",
    "\x7f",
    "コンサート 第1部",
]


def legacy_cluster_width(cluster: str) -> int:
    if cluster == "":
        return 0
    if all(_is_zero(char) for char in cluster):
        return 0
    wide = False
    for char in cluster:
        if _is_zero(char):
            continue
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            wide = True
    return 2 if wide else 1


def legacy_truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    kept: list[str] = []
    used = 0
    for cluster in graphemes(text):
        used += legacy_cluster_width(cluster)
        if used > width:
            return "".join(kept)
        kept.append(cluster)
    return text


def test_cluster_and_display_width_match_unicodedata_walk():
    for sample in SAMPLES:
        assert cluster_width(sample) == legacy_cluster_width(sample)
        assert display_width(sample) == sum(
            legacy_cluster_width(cluster) for cluster in graphemes(sample)
        )


@pytest.mark.parametrize("width", [0, 1, 2, 3, 5, 10, 40])
def test_truncate_width_matches_legacy(width: int):
    for sample in SAMPLES:
        assert truncate_width(sample, width) == legacy_truncate(sample, width)


def test_truncate_width_keeps_zero_width_control_characters():
    # ASCII 高速経路は制御文字（幅0）を通さない。
    assert truncate_width("a\x01b", 2) == "a\x01b"


def test_cluster_width_is_cached():
    cluster_width.cache_clear()
    assert cluster_width("あ") == 2
    assert cluster_width("あ") == 2
    info = cluster_width.cache_info()
    assert info.misses == 1
    assert info.hits == 1
    cluster_width.cache_clear()


def legacy_handle(editor: LineEditor, key: str) -> None:
    from radiko_client.tui import _boundaries, _snap_points

    points = _boundaries(editor.value)
    editor.cursor = _snap_points(points, editor.cursor)
    raw = editor.value.encode()
    index = points.index(editor.cursor)
    if key == "ctrl-u":
        editor.value = ""
        editor.cursor = 0
    elif key == "backspace":
        start = points[index - 1] if index > 0 else 0
        editor.value = (raw[:start] + raw[editor.cursor :]).decode()
        editor.cursor = start
    elif key == "delete":
        end = points[index + 1] if index + 1 < len(points) else len(raw)
        editor.value = (raw[: editor.cursor] + raw[end:]).decode()
    elif key == "left":
        editor.cursor = points[index - 1] if index > 0 else 0
    elif key == "right":
        editor.cursor = points[index + 1] if index + 1 < len(points) else len(raw)
    elif key == "home":
        editor.cursor = 0
    elif key == "end":
        editor.cursor = len(raw)
    elif len(key) == 1 and key.isprintable() and not key.isspace():
        if len(raw) + len(key.encode()) <= editor.byte_limit:
            editor.value = (raw[: editor.cursor] + key.encode() + raw[editor.cursor :]).decode()
            editor.cursor += len(key.encode())
    editor.cursor = _snap_points(
        _boundaries(editor.value), min(editor.cursor, len(editor.value.encode()))
    )


@pytest.mark.parametrize(
    "keys",
    [
        ["あ", "a", "\u0301", "い", "backspace", "left", "delete"],
        ["a", "b", "left", "left", "right", "end", "home", "delete"],
        ["👨‍👩‍👧", "x", "\u0301", "backspace", "ctrl-u"],
        ["🇯", "🇵", "left", "left", "right", "backspace"],
        ["ｱ", "α", "Ａ", "end", "backspace", "backspace"],
    ],
)
def test_line_editor_matches_single_pass_reference(keys: list[str]):
    editor = LineEditor.new("", 1024)
    reference = LineEditor.new("", 1024)
    for key in keys:
        editor.handle(key)
        legacy_handle(reference, key)
        assert (editor.value, editor.cursor) == (reference.value, reference.cursor)


def make_programs(count: int) -> list[Program]:
    start = datetime(2026, 10, 15, 10, 0, tzinfo=JST)
    return [
        Program(ProgramKey("TBS", start), f"id{index}", start + timedelta(hours=1), f"番組{index}")
        for index in range(count)
    ]


def test_visible_is_filters_once_and_reuses_until_programs_or_filter_change(monkeypatch):
    calls = {"count": 0}
    original = Filter.matches

    def counting(self, program):
        calls["count"] += 1
        return original(self, program)

    monkeypatch.setattr(Filter, "matches", counting)
    programs = make_programs(50)
    screen = Screen(programs, programs[0].key.starts_at, width=80, height=20)

    rows = screen.visible()
    assert len(rows) == 50
    assert calls["count"] == 50
    assert screen.visible() is rows
    screen.render()
    assert calls["count"] == 50

    # 絞り込み条件を差し替えると再評価する。
    screen.committed = Filter.create(keyword="番組1")
    assert len(screen.visible()) == 11
    assert calls["count"] == 50 + 50

    # 番組一覧を差し替えると再評価する。
    screen.committed = Filter.create()
    screen.programs = make_programs(5)
    assert len(screen.visible()) == 5
    assert calls["count"] == 100 + 5


def test_line_editor_resnaps_after_deleting_a_format_character():
    # ZWSP を消すと前後の文字が1クラスタに融合し、元のカーソル位置は境界でなくなる。
    editor = LineEditor("Ａ\u200b\u3099x", 3, 4096)
    editor.handle("delete")
    assert editor.value == "Ａ\u3099x"
    assert editor.cursor == 0
    assert editor.cursor in _boundaries(editor.value)

    editor = LineEditor("e\u200b\u0301z", 4, 4096)
    editor.handle("backspace")
    assert editor.value == "e\u0301z"
    assert editor.cursor == 0
    assert editor.cursor in _boundaries(editor.value)
