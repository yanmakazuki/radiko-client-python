"""TUI のセル。Ratatui の罫線や色までは一致させず、幅・書記素・文面を固定する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from radiko.domain import JST
from radiko.text import display_width
from radiko_client.tui import Screen, offline_programs


def _screen(width: int, height: int) -> Screen:
    now = datetime(2026, 10, 15, 6, 30, tzinfo=JST)
    programs = offline_programs("TBS", now.date())
    programs[0] = replace(programs[0], title="\u304b\u3099家族")
    screen = Screen(programs, now, width=width, height=height, offline=True)
    screen.selected = 0
    return screen


def test_normal_compact_and_narrow_cells_keep_japanese_graphemes():
    normal = _screen(80, 40)
    grid = normal.cells()
    assert len(grid) == 40
    assert all(len(row) == 80 for row in grid)
    text = "\n".join("".join(row) for row in grid)
    assert "番組一覧 一致" in text
    assert "\u304b\u3099家族" in text
    assert "オフライン確認" in text

    compact = _screen(80, 29)
    compact_text = "\n".join("".join(row) for row in compact.cells())
    assert "番組一覧 一致" not in compact_text
    assert "\u304b\u3099家族" in compact_text

    narrow = _screen(5, 20)
    line = next(item for item in narrow.render() if "\u304b\u3099" in item)
    assert "家" not in line
    assert display_width(line) <= 5
    cells = narrow.cells()
    assert all(len(row) == 5 for row in cells)
    joined = "".join(cell for row in cells for cell in row)
    assert "\u3099" in joined
    assert "\u5bb6" not in joined


def test_help_overlay_uses_the_width_and_keeps_the_close_hint():
    screen = _screen(20, 12)
    screen.area = "help"
    rows = screen.render()
    assert len(rows) == 12
    assert all(display_width(row) <= 20 for row in rows)
    assert any("q/Esc閉じる" in row for row in rows)
    assert any("番組を探す" in row for row in rows)
    screen.handle("q")
    assert screen.area == "list"
    assert screen.quit is False
