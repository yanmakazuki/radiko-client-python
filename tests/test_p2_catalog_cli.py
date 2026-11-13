"""Issue #30: 番組表の単一パス抽出、CLI 出力の直結、時刻整形の契約を固定する。"""

from __future__ import annotations

import io
import json
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from radiko import catalog, domain, stream
from radiko.catalog import ProgramList
from radiko.domain import JST, Classification, ObservedClassifications, Program, ProgramKey
from radiko_client import cli

DAY = date(2026, 10, 15)


def schedule(body: str) -> bytes:
    return (
        f'<radiko><stations><station id="TBS"><name>ＴＢＳ テスト</name><progs>{body}'
        "</progs></station></stations></radiko>"
    ).encode()


def program_xml(**fields: str) -> str:
    attributes = dict(
        id="p1",
        ft="20261015060000",
        to="20261015070000",
        title="朝の番組",
        pfm="出演者",
        desc="説明",
        info="情報",
    )
    attributes.update(fields)
    children = "".join(
        f"<{name}>{value}</{name}>"
        for name, value in attributes.items()
        if name not in {"id", "ft", "to"} and value
    )
    return f'<prog id="{attributes["id"]}" ft="{attributes["ft"]}" to="{attributes["to"]}">{children}</prog>'


def test_parse_programs_keeps_first_child_and_merges_genre_in_one_pass():
    body = (
        '<prog id="p1" ft="20261015060000" to="20261015070000">'
        "<title>最初</title><title>あとから</title>"
        "<pfm>出演</pfm><pfm>別人</pfm>"
        '<genre><personality id="P1"><name>タレント</name></personality>'
        '<program id="G1"><name>音楽</name></program>'
        '<program id="G1"><name>音楽</name></program></genre>'
        "<tag><item><name>タグ1</name></item><item><name>タグ1</name></item></tag>"
        "<tag><item><name>タグ2</name></item></tag></prog>"
    )
    parsed = catalog.parse_programs(schedule(body), "TBS", DAY)
    assert len(parsed.programs) == 1
    item = parsed.programs[0]
    assert item.title == "最初"
    assert item.performer == "出演"
    assert item.tags == ["タグ1", "タグ2"]
    assert [(entry.id, entry.name) for entry in item.program_genres] == [("G1", "音楽")]
    assert [(entry.id, entry.name) for entry in item.personality_genres] == [("P1", "タレント")]


def test_parse_programs_without_optional_children_uses_defaults():
    body = '<prog id="p1" ft="20261015060000" to="20261015070000"></prog>'
    parsed = catalog.parse_programs(schedule(body), "TBS", DAY)
    item = parsed.programs[0]
    assert (item.title, item.performer, item.description, item.info) == ("無題", "", "", "")
    assert item.tags == []
    assert item.program_genres == []
    assert item.personality_genres == []


def test_parse_programs_ignores_later_genre_and_text_nodes():
    body = (
        '<prog id="p1" ft="20261015060000" to="20261015070000">'
        '<genre><program id="G1"><name>音楽</name></program></genre>'
        '<genre><program id="G2"><name>報道</name></program></genre>'
        "<title>題</title></prog>"
    )
    parsed = catalog.parse_programs(schedule(body), "TBS", DAY)
    assert [entry.id for entry in parsed.programs[0].program_genres] == ["G1"]


def make_listing(count: int = 3) -> ProgramList:
    start = datetime(2026, 10, 15, 6, 0, tzinfo=JST)
    programs = [
        Program(
            key=ProgramKey("TBS", start + timedelta(hours=index)),
            program_id=f"p{index}",
            ends_at=start + timedelta(hours=index + 1),
            title=f"番組{index}",
            performer=f"出演{index}",
            tags=["タグ"],
            program_genres=[Classification("G1", "音楽")],
            personality_genres=[] if index == 1 else [Classification("P1", "タレント")],
        )
        for index in range(count)
    ]
    return ProgramList("TBS", "TBS", DAY, programs)


def test_print_list_does_not_build_a_snapshot(monkeypatch):
    listing = make_listing()
    now = datetime(2026, 10, 15, 7, 30, tzinfo=JST)

    def forbidden(*args, **kwargs):
        raise AssertionError("snapshot must not be built for text output")

    monkeypatch.setattr(cli, "_snapshot", forbidden)
    monkeypatch.setattr(ObservedClassifications, "collect", forbidden)
    buffer = io.StringIO()
    original = sys.stdout
    sys.stdout = buffer
    try:
        cli._print_list(listing, now, domain.Filter.create(keyword="番組1"))
    finally:
        sys.stdout = original
    lines = buffer.getvalue().splitlines()
    assert lines[0] == "TBS (TBS) / 放送日 2026-10-15 / 日本時間（実行時点の状態）"
    assert lines[1].startswith("2026-10-15T07:00:00+09:00–2026-10-15T08:00:00+09:00 [")
    assert lines[2] == "  出演: 出演1 / 番組ジャンル: 音楽 / 出演者分類: 未分類"
    assert (
        lines[3] == "一致1件 / 取得3件 / 不正・日付不一致: 0件 / 重複: 0件 / 全件: 再生可否未確認"
    )


def test_merge_observed_classifications_matches_collecting_all_programs():
    left = make_listing(3)
    right = make_listing(3)
    right.programs = [
        Program(
            key=ProgramKey("QRR", item.key.starts_at),
            program_id=item.program_id,
            ends_at=item.ends_at,
            title=item.title,
            program_genres=[Classification("G2", "報道")],
            personality_genres=[Classification("P2", "アナウンサー")],
        )
        for item in right.programs
    ]
    merged = ObservedClassifications.merge(
        [
            ObservedClassifications.collect(left.programs),
            ObservedClassifications.collect(right.programs),
        ]
    )
    whole = ObservedClassifications.collect(left.programs + right.programs)
    assert merged == whole


def test_report_snapshot_observations_match_the_program_set():
    listing = make_listing(3)
    now = datetime(2026, 10, 15, 7, 30, tzinfo=JST)
    request = cli.ProgramRequest.__new__(cli.ProgramRequest)
    request.filter = domain.Filter.create()
    request.range = domain.BroadcastRange(DAY, DAY, [DAY])
    report = catalog.Report(areas=["JP13"], lists=[listing, listing])
    snapshot = cli._report_snapshot(report, request, now)
    expected = ObservedClassifications.collect(listing.programs + listing.programs)
    assert snapshot["observed_classifications"] == cli._observed(expected)
    assert snapshot["matched_program_count"] == 6
    json.dumps(snapshot, ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("20261015060000", datetime(2026, 10, 15, 6, 0, tzinfo=JST)),
        ("20260229060000", None),
        ("20261301060000", None),
        ("2026101506000", None),
        ("202610150600000", None),
        ("2026101506000a", None),
        ("", None),
    ],
)
def test_parse_time_matches_the_reference_contract(text: str, expected: datetime | None):
    if expected is None:
        with pytest.raises(stream.ProbeError) as caught:
            stream.parse_time(text)
        assert caught.value.code == "input"
    else:
        assert stream.parse_time(text) == expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc), "2026-10-15T05:00:00+00:00"),
        (datetime(2026, 10, 15, 14, 0, tzinfo=JST), "2026-10-15T14:00:00+09:00"),
        (
            datetime(2026, 10, 15, 14, 0, 0, 123000, tzinfo=JST),
            "2026-10-15T14:00:00.123+09:00",
        ),
        (
            datetime(2026, 10, 15, 14, 0, 0, 123456, tzinfo=JST),
            "2026-10-15T14:00:00.123456+09:00",
        ),
        (
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=-5))),
            "2026-01-01T00:00:00-05:00",
        ),
        (
            datetime(2026, 3, 1, 0, 0, 0, 1, tzinfo=timezone(timedelta(hours=5, minutes=45))),
            "2026-03-01T00:00:00.000001+05:45",
        ),
    ],
)
def test_rfc3339_keeps_the_offset_and_fraction(moment: datetime, expected: str):
    assert domain.rfc3339(moment) == expected


def test_rfc3339_treats_a_naive_value_as_utc():
    assert domain.rfc3339(datetime(2026, 10, 15, 14, 0)) == "2026-10-15T14:00:00+00:00"
