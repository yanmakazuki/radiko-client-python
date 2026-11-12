"""P0（#27）: 番組時刻・不連続番号の1パス索引が従来の走査と同じ値を返すことを固定する。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from radiko import prepare, timing
from radiko.chapters import MAX_CHAPTER_BYTES, chapters_from_json, parse_chapters
from radiko.domain import JST
from radiko.hls import MediaPlaylist, Segment, segment_discontinuity, segment_schedule

BASE = datetime(2026, 10, 15, 6, 0, 0, tzinfo=JST)


def _segments() -> list[Segment]:
    rows = []
    for index in range(12):
        date = None
        if index in {0, 5, 9}:
            date = (BASE + timedelta(seconds=index * 5)).isoformat()
        rows.append(
            Segment(
                f"s{index}.aac",
                5.0,
                discontinuity=index in {3, 8},
                program_date=date,
            )
        )
    return rows


def _media(sequence: int = 1000, base: int = 7) -> MediaPlaylist:
    return MediaPlaylist(sequence, 5, _segments(), end_list=True, discontinuity_sequence=base)


def _naive_wall(media: MediaPlaylist, sequence: int) -> datetime | None:
    stamp = None
    cursor = media.media_sequence
    for segment in media.segments:
        if segment.program_date:
            stamp = datetime.fromisoformat(segment.program_date.replace("Z", "+00:00"))
        if cursor == sequence:
            return stamp
        if stamp is not None:
            stamp = stamp + timedelta(seconds=segment.duration)
        cursor += 1
    return None


def _naive_discontinuity(media: MediaPlaylist, sequence: int) -> int | None:
    count = 0
    cursor = media.media_sequence
    for segment in media.segments:
        if segment.discontinuity:
            count += 1
        if cursor == sequence:
            return media.discontinuity_sequence + count
        cursor += 1
    return None


def _naive_apply_seek(control, media, sequence, media_seconds, origin, pending):
    if not control.accepted or control.applied == len(control.accepted):
        return sequence, media_seconds, pending, None
    target = control.accepted[-1]
    control.applied = len(control.accepted)
    target_at = origin + timedelta(seconds=target)
    cursor = media.media_sequence
    walls = []
    for segment in media.segments:
        wall = _naive_wall(media, cursor)
        if wall is not None:
            walls.append((wall, segment.duration))
        cursor += 1
    if walls and (
        target_at < walls[0][0] or target_at >= walls[-1][0] + timedelta(seconds=walls[-1][1])
    ):
        return sequence, media_seconds, pending, target
    cursor = media.media_sequence
    for segment in media.segments:
        wall = _naive_wall(media, cursor)
        if wall is not None and target_at >= wall + timedelta(seconds=segment.duration):
            cursor += 1
            continue
        break
    return cursor, target, (target_at, target), None


def test_direct_playlist_wall_and_discontinuity_match_the_naive_walk():
    media = _media()
    for sequence in range(990, 1020):
        assert timing.segment_wall(media, sequence) == _naive_wall(media, sequence)
        assert segment_discontinuity(media, sequence) == _naive_discontinuity(media, sequence)
        assert prepare._segment_discontinuity(media, sequence) == _naive_discontinuity(
            media, sequence
        )


def test_schedule_reuses_the_table_until_the_window_changes():
    media = _media()
    first = segment_schedule(media)
    assert segment_schedule(media) is first
    media.segments = _segments()[2:]
    media.media_sequence = 1002
    media.discontinuity_sequence = 9
    second = segment_schedule(media)
    assert second is not first
    assert segment_discontinuity(media, 1002) == 9
    assert timing.segment_wall(media, 1002) is None
    assert timing.segment_wall(media, 1005) == BASE + timedelta(seconds=25)


def test_apply_seek_matches_the_two_pass_reference():
    media = _media()
    for target in (0.0, 2.0, 5.0, 12.5, 35.0, 59.5, 60.0):
        expected_control = SimpleNamespace(accepted=[target], applied=0)
        actual_control = SimpleNamespace(accepted=[target], applied=0)
        expected = _naive_apply_seek(expected_control, media, 1000, 0.0, BASE, None)
        actual = prepare._apply_seek(actual_control, media, 1000, 0.0, BASE, None)
        assert actual == expected


def test_chapters_from_json_matches_parse_chapters():
    body = json.dumps(
        {
            "stationId": "TBS",
            "programId": "evt",
            "chapters": [
                {"fromSec": 10, "toSec": 20, "title": "A"},
                {"fromSec": 30, "toSec": 40, "isMusic": True},
                {"fromSec": 50, "toSec": 40, "title": "bad"},
            ],
        }
    ).encode()
    expected = parse_chapters(body, 90.0, 0.0)
    actual = chapters_from_json(json.loads(body), 90.0, 0.0)
    assert [(item.start, item.end, item.title) for item in actual.chapters] == [
        (item.start, item.end, item.title) for item in expected.chapters
    ]
    assert (actual.music_count, actual.invalid_count) == (
        expected.music_count,
        expected.invalid_count,
    )


def test_inspect_remote_chapters_decodes_the_body_once(monkeypatch):
    program = prepare.ResolvedProgram(
        "P0",
        BASE,
        BASE + timedelta(hours=1),
        (True, True, True, True),
        False,
        "evt",
        BASE,
        "TBS",
    )
    body = json.dumps(
        {
            "stationId": "TBS",
            "programId": "evt",
            "chapters": [{"fromSec": 10, "toSec": 20, "title": "A"}],
        }
    ).encode()
    monkeypatch.setattr(prepare, "fetch_program", lambda *args: program)
    monkeypatch.setattr(prepare, "stream_get", lambda *args, **kwargs: SimpleNamespace(body=body))
    calls: list[bytes] = []
    original = json.loads

    def counting_loads(data, *args, **kwargs):
        calls.append(data)
        return original(data, *args, **kwargs)

    monkeypatch.setattr(json, "loads", counting_loads)
    view = prepare.inspect_remote_chapters("TBS", BASE)
    assert len(calls) == 1
    assert view.kind == "aligned"
    assert view.count == 1


def test_inspect_remote_chapters_keeps_the_size_limit(monkeypatch):
    program = prepare.ResolvedProgram(
        "P0",
        BASE,
        BASE + timedelta(hours=1),
        (True, True, True, True),
        False,
        "evt",
        BASE,
        "TBS",
    )
    body = (
        b'{"stationId":"TBS","programId":"evt","chapters":[],"pad":"'
        + b"x" * MAX_CHAPTER_BYTES
        + b'"}'
    )
    monkeypatch.setattr(prepare, "fetch_program", lambda *args: program)
    monkeypatch.setattr(prepare, "stream_get", lambda *args, **kwargs: SimpleNamespace(body=body))
    with pytest.raises(prepare.ChapterFailure) as raised:
        prepare.inspect_remote_chapters("TBS", BASE)
    assert "サイズ上限" in str(raised.value)
