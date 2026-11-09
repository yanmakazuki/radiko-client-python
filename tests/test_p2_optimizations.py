"""P2（Issue #6）の最適化が旧実装と同じ結果になることを固定する。"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timedelta, timezone

import pytest

from radiko.catalog import (
    MAX_DEPTH,
    CatalogError,
    _parse_document,
    parse_clock,
    parse_programs,
)
from radiko.domain import VALID_AREAS, Filter, Program, ProgramKey
from radiko.text import clean_text, has_control

JST = timezone(timedelta(hours=9))


def legacy_clean_text(text: str) -> str:
    cleaned = []
    for char in text:
        if char.isspace():
            cleaned.append(" ")
        elif unicodedata.category(char) == "Cc":
            continue
        else:
            cleaned.append(char)
    return " ".join("".join(cleaned).split())


def legacy_has_control(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in text)


def test_clean_text_matches_legacy_for_every_control_codepoint():
    samples = [chr(code) for code in range(0x00, 0xA0)]
    samples += ["\u3000", "\u00a0", "\u2028", "\u2029", "\u0085", "\u200b"]
    for char in samples:
        assert clean_text(char) == legacy_clean_text(char)
        assert clean_text(f"a{char}b") == legacy_clean_text(f"a{char}b")
        assert clean_text(f" {char} a {char} ") == legacy_clean_text(f" {char} a {char} ")
    assert clean_text("  番組  \t 名\n") == "番組 名"


def test_has_control_matches_legacy_for_every_control_codepoint():
    for code in range(0x00, 0xA0):
        char = chr(code)
        assert has_control(char) == legacy_has_control(char)
    assert has_control("ふつうの文字") is False
    assert has_control("a\u0085b") is True


def test_parse_clock_matches_strptime_contract():
    valid = ["20261015050000", "20240229235959", "20260101000000"]
    for text in valid:
        expected = datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=JST)
        assert parse_clock(text) == expected
    for text in [
        "2026101505000",
        "202610150500000",
        "20261315050000",
        "2026101505000a",
        "２０２６１０１５０５００００",
        "",
    ]:
        with pytest.raises(ValueError):
            parse_clock(text)


def test_valid_areas_is_the_same_range_as_before():
    assert VALID_AREAS == frozenset(f"JP{number}" for number in range(1, 48))
    assert "JP1" in VALID_AREAS
    assert "JP47" in VALID_AREAS
    assert "JP0" not in VALID_AREAS
    assert "JP48" not in VALID_AREAS


def test_parse_document_keeps_xxe_depth_and_structure_contracts():
    assert _parse_document("<radiko/>", "radiko").tag == "radiko"
    assert (
        _parse_document("<?xml version='1.0'?><!-- c --><radiko><a/></radiko>", "radiko")
        is not None
    )
    for text in [
        "<!doctype radiko><radiko/>",
        "<!DOCTYPE radiko><radiko/>",
        "<!ENTITY x 'y'><radiko/>",
        "<stations/>",
        "<radiko>",
        "<radiko></stations>",
        "<radiko/>tail",
        "",
    ]:
        with pytest.raises(CatalogError):
            _parse_document(text, "radiko")

    exact = "<radiko>" + "<a>" * (MAX_DEPTH - 1) + "</a>" * (MAX_DEPTH - 1) + "</radiko>"
    assert _parse_document(exact, "radiko") is not None
    too_deep = "<radiko>" + "<a>" * MAX_DEPTH + "</a>" * MAX_DEPTH + "</radiko>"
    with pytest.raises(CatalogError):
        _parse_document(too_deep, "radiko")


def test_program_list_parsing_still_rejects_bad_xml():
    with pytest.raises(CatalogError):
        parse_programs(b"<radiko/>", "TBS", date(2026, 10, 15))
    with pytest.raises(CatalogError):
        parse_programs(b"<!DOCTYPE radiko><radiko/>", "TBS", date(2026, 10, 15))


def test_filter_uses_frozenset_genres_and_cached_search_text():
    start = datetime(2026, 10, 15, 10, 0, tzinfo=JST)
    program = Program(
        ProgramKey("TBS", start),
        "id",
        start + timedelta(hours=1),
        "朝の番組",
        performer="パーソナリティ",
        description="本文",
        tags=["タグ"],
    )
    genre_filter = Filter.create(["A", "A", "B"], [], False, False, None)
    assert genre_filter.program_genre_set == frozenset({"A", "B"})
    assert genre_filter.personality_genre_set == frozenset()

    keyword_filter = Filter.create([], [], False, False, "パーソナリティ")
    assert program._search_text is None
    assert keyword_filter.matches(program) is True
    cached = program._search_text
    assert cached is not None and "パーソナリティ" in cached
    assert program.search_text is cached

    assert Filter.create([], [], False, False, "本文タグ").matches(program) is False
    assert Filter.create([], [], False, False, "タグ").matches(program) is True
    assert keyword_filter.to_dict()["keyword"] == "パーソナリティ"


# --- Issue #7: TUI の表示幅計算 ---

from radiko.text import display_width, graphemes  # noqa: E402
from radiko_client.tui import (  # noqa: E402
    LineEditor,
    Screen,
    help_text,
    truncate_width,
    wrap_display,
)


def legacy_truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    kept = ""
    for cluster in graphemes(text):
        if display_width(kept + cluster) > width:
            break
        kept += cluster
    return kept


def legacy_wrap(text: str, width: int) -> list[str]:
    if text == "":
        return [""]
    if width == 0:
        return [text]
    lines: list[str] = []
    current = ""
    used = 0
    for cluster in graphemes(text):
        size = display_width(cluster)
        if current and used + size > width:
            lines.append(current)
            current = ""
            used = 0
        current += cluster
        used += size
    lines.append(current)
    return lines


def legacy_view(value: str, cursor: int, columns: int) -> tuple[str, int]:
    clusters = graphemes(value)
    index = 0
    cursor_cluster = 0
    for position, cluster in enumerate(clusters):
        if index >= cursor:
            cursor_cluster = position
            break
        index += len(cluster.encode())
    else:
        cursor_cluster = len(clusters)
    if columns <= 0:
        return value, 0
    start = 0
    while (
        display_width("".join(clusters[start:cursor_cluster])) >= columns and start < cursor_cluster
    ):
        start += 1
    end = cursor_cluster
    while end < len(clusters) and display_width("".join(clusters[start : end + 1])) <= columns:
        end += 1
    text = "".join(clusters[start:end])
    column = display_width("".join(clusters[start:cursor_cluster]))
    if column >= columns:
        column = max(columns - 1, 0)
    return text, column


WIDTH_TEXTS = [
    "",
    "a",
    "あいうえお",
    "番組名です",
    "a\u0301b\u0301c",
    "👨‍👩‍👧‍👦家族",
    "𠮷野家のうなぎ",
    "ＡＢＣ１２３",
    "e\u0301\u0327x",
]


def test_truncate_width_and_wrap_display_match_legacy():
    for text in WIDTH_TEXTS:
        for width in range(0, 14):
            assert truncate_width(text, width) == legacy_truncate(text, width)
            assert wrap_display(text, width) == legacy_wrap(text, width)


def test_line_editor_view_matches_legacy_for_every_cursor():
    for text in WIDTH_TEXTS:
        points = [0]
        for cluster in graphemes(text):
            points.append(points[-1] + len(cluster.encode()))
        for cursor in points:
            editor = LineEditor(text, cursor, 4096)
            for columns in range(0, 10):
                assert editor.view(columns) == legacy_view(text, cursor, columns)


def test_line_editor_editing_keeps_cursor_on_grapheme_boundaries():
    editor = LineEditor.new("", 1024)
    for char in ["あ", "a", "\u0301", "い"]:
        editor.handle(char)
    assert editor.value == "あa\u0301い"
    editor.handle("backspace")
    assert editor.value == "あa\u0301"
    editor.handle("left")
    editor.handle("left")
    editor.handle("delete")
    assert editor.value == "a\u0301"
    for point in [0, len(editor.value.encode())]:
        editor.cursor = point
        editor.handle("left")
        assert 0 <= editor.cursor <= len(editor.value.encode())


def test_help_text_is_cached_and_render_calls_visible_once(monkeypatch):
    assert help_text() is help_text()
    assert help_text.cache_info().maxsize == 1

    start = datetime(2026, 10, 15, 10, 0, tzinfo=JST)
    program = Program(ProgramKey("TBS", start), "id", start + timedelta(hours=1), "番組")
    screen = Screen([program], start, width=40, height=20, area="keyword")
    calls = {"count": 0}
    original = Screen.visible

    def counting(self):
        calls["count"] += 1
        return original(self)

    monkeypatch.setattr(Screen, "visible", counting)
    rows = screen.render()
    assert rows
    assert calls["count"] == 1
    assert screen.keyword_preview() is not None
    assert calls["count"] == 2


# --- Issue #8: MP4 / ADTS パーサ ---

import tracemalloc  # noqa: E402

from radiko.fragmented import _atoms, box, words  # noqa: E402
from radiko.fragmented import _word as atom_word  # noqa: E402
from radiko.media import _parse_adts, audio_offset, split_adts, validate_mp4  # noqa: E402


def adts_frame(payload: bytes, rate_index: int = 4, channels: int = 2) -> bytes:
    length = 7 + len(payload)
    header = bytearray(7)
    header[0] = 0xFF
    header[1] = 0xF1
    header[2] = (1 << 6) | (rate_index << 2) | ((channels >> 2) & 1)
    header[3] = ((channels & 3) << 6) | ((length >> 11) & 3)
    header[4] = (length >> 3) & 0xFF
    header[5] = ((length & 7) << 5) | 0x1F
    header[6] = 0xFC
    return bytes(header) + payload


def legacy_split_adts(data: bytes) -> list[tuple[int, int, int, bytes]]:
    remaining = data[audio_offset(data) :]
    frames = []
    while remaining:
        header = _parse_adts(remaining)
        frames.append(
            (
                header["object_type"],
                header["sample_rate"],
                header["channels"],
                remaining[header["header_len"] : header["frame_length"]],
            )
        )
        remaining = remaining[header["frame_length"] :]
    return frames


def legacy_atoms(data: bytes) -> list[tuple[bytes, int, int, bytes]]:
    result = []
    at = 0
    while at < len(data):
        rest = data[at:]
        size = atom_word(rest, 0)
        kind = rest[4:8]
        if size == 0:
            size, header = len(rest), 8
        elif size == 1:
            size = (atom_word(rest, 8) << 32) | atom_word(rest, 12)
            header = 16
        else:
            header = 8
        result.append((kind, at, header, rest[header:size]))
        at += size
    return result


def test_split_adts_matches_legacy_and_returns_bytes_payloads():
    frames = [
        adts_frame(b"\x01" * 5),
        adts_frame(b"\x02" * 37),
        adts_frame(b"\x03" * 1),
        adts_frame(b"\x04" * 257),
    ]
    data = b"".join(frames)
    parsed = split_adts(data, lambda: False)
    expected = legacy_split_adts(data)
    assert [
        (frame.object_type, frame.sample_rate, frame.channels, frame.payload) for frame in parsed
    ] == expected
    assert all(isinstance(frame.payload, bytes) for frame in parsed)

    skipping = b"ID3\x04\x00\x00" + bytes([0, 0, 0, 16]) + b"\x00" * 16 + data
    assert [frame.payload for frame in split_adts(skipping, lambda: False)] == [
        item[3] for item in expected
    ]


def test_atoms_matches_legacy_for_nested_and_large_boxes():
    large_64 = (1).to_bytes(4, "big") + b"mdat" + (23).to_bytes(8, "big") + b"payload"
    data = (
        box(b"ftyp", b"isom")
        + box(b"moov", box(b"trak", words(0, 1)) + box(b"free", b""))
        + large_64
        + box(b"skip", b"x")
    )
    parsed = _atoms(data)
    expected = legacy_atoms(data)
    assert [(item.kind, item.offset, item.header, bytes(item.data)) for item in parsed] == expected
    assert parsed[0].kind == b"ftyp"
    assert parsed[2].header == 16


def test_mp4_walk_does_not_copy_the_remaining_segment():
    payload = b"\x00" * (4 * 1024 * 1024)
    data = box(b"ftyp", b"isom") + (len(payload) + 8).to_bytes(4, "big") + b"mdat" + payload
    tracemalloc.start()
    try:
        validate_mp4(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024


def test_atoms_does_not_copy_the_remaining_segment():
    payload = b"\x00" * (4 * 1024 * 1024)
    data = (len(payload) + 8).to_bytes(4, "big") + b"mdat" + payload
    tracemalloc.start()
    try:
        atoms = _atoms(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(atoms) == 1
    assert peaks_below(peak)


def peaks_below(peak: int) -> bool:
    return peak < 512 * 1024
