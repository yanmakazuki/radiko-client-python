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


def test_row_allocation_matches_the_rust_compact_boundary():
    """高さ 29 は見出し 2 行で一覧が 2 行目、30 は 7 行の見出しの次に一覧が来る。"""
    from datetime import timedelta

    compact = _screen(80, 29)
    compact.programs[0] = replace(
        compact.programs[0], ends_at=compact.programs[0].key.starts_at + timedelta(seconds=3300)
    )
    compact.playing = compact.programs[0]
    compact.position = 754
    rows = compact.render()
    assert "番組一覧" in rows[2]
    assert "番組一覧 一致" not in "\n".join(rows)
    assert "[TBS / タイムフリー]" in rows[-3]
    assert "12:34 / 55:00" in rows[-3]
    assert "音量" not in rows[-3]
    assert rows[-1] == "? 操作一覧 / Enter 再生 / Space 一時停止 / q 終了"

    normal = _screen(80, 30)
    normal.playing = normal.programs[0]
    normal.position = 754
    normal.programs[0] = replace(
        normal.programs[0], ends_at=normal.programs[0].key.starts_at + timedelta(seconds=3300)
    )
    normal.playing = normal.programs[0]
    body = normal.render()
    assert "番組一覧" in body[7]
    assert "音量25%" in "\n".join(body)
    assert "e 取得状況" in body[-1]
    assert "? 操作一覧 / Enter 再生 / Space 一時停止 / q 終了" not in "\n".join(body)
    assert body[0].startswith("┌")
    assert body[6].startswith("└")


def test_login_form_hides_the_password_and_colors_an_error():
    screen = _screen(40, 12)
    screen.offline = False
    screen.area = "login"
    for char in "user@example.invalid":
        screen.handle(char)
    screen.handle("enter")
    for char in "SYNTHETIC_PASSWORD_99":
        screen.handle(char)
    text = "\n".join(screen.render())
    assert "user@example.invalid" in text
    assert "入力済み（非表示）" in text
    assert "SYNTHETIC_PASSWORD_99" not in text
    assert "┌ ログイン（パスワード非表示）" in text
    screen.handle("esc")
    screen.notice = screen.notice or None
    from radiko_client.tui import Notice, NoticeLevel

    screen.notice = Notice(NoticeLevel.ERROR, "失敗", True, 0)
    assert screen.notice_color() == "red"
    screen.no_color = True
    assert screen.notice_color() is None


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
