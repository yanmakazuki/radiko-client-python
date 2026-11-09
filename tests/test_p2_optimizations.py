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
