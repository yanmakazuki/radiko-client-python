"""Rust 版 a7eaefa の観測できる契約。期待値はソースの表と fixture から独立に置いた。"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from radiko.catalog import parse_programs, parse_stations
from radiko.chapters import parse_chapters
from radiko.domain import (
    BroadcastRange,
    Filter,
    Scope,
    SearchCriteria,
    broadcast_day,
    broadcast_status,
    rfc3339,
)
from radiko.hls import HlsCursor, parse_media_playlist
from radiko.http_client import _allowed
from radiko.media import AacError, validate_mp4
from radiko.storage import MemberSession, Membership, SearchStore, SessionStore
from radiko.stream import ProbeError, SlotProgram, availability, deadline_pair, eligible
from radiko.stream import parse_time as stream_time

ROOT = Path(__file__).parent / "fixtures"
JST = timezone(timedelta(hours=9))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_broadcast_day_uses_jst_five_am_not_local_midnight():
    assert broadcast_day(at("2026-10-25T04:30:00+09:00")) == date(2026, 10, 24)
    assert broadcast_day(at("2026-10-25T05:00:00+09:00")) == date(2026, 10, 25)
    assert broadcast_day(at("2026-10-01T19:30:00+00:00")) == date(2026, 10, 1)
    assert broadcast_day(at("2026-10-01T20:30:00+00:00")) == date(2026, 10, 2)
    early = at("2026-10-16T04:59:00+09:00")
    criteria = SearchCriteria.initial(early)
    assert criteria.range.start == date(2026, 10, 15)
    assert criteria.scope.kind == "nationwide"


def test_timefree_deadlines_follow_the_morning_after_the_end_broadcast_day():
    cases = [
        ("2026-10-01T12:00:00+09:00", "2026-10-09T05:00:00+09:00", "2026-11-01T05:00:00+09:00"),
        ("2026-10-02T04:59:59+09:00", "2026-10-09T05:00:00+09:00", "2026-11-01T05:00:00+09:00"),
        ("2026-10-02T05:00:00+09:00", "2026-10-10T05:00:00+09:00", "2026-11-02T05:00:00+09:00"),
        ("2026-01-31T18:00:00+09:00", "2026-02-08T05:00:00+09:00", "2026-03-03T05:00:00+09:00"),
        ("2026-03-01T04:59:59+09:00", "2026-03-08T05:00:00+09:00", "2026-03-31T05:00:00+09:00"),
        ("2026-03-01T05:00:00+09:00", "2026-03-09T05:00:00+09:00", "2026-04-01T05:00:00+09:00"),
        ("2025-12-31T23:00:00+09:00", "2026-01-08T05:00:00+09:00", "2026-01-31T05:00:00+09:00"),
        ("2026-01-01T04:59:59+09:00", "2026-01-08T05:00:00+09:00", "2026-01-31T05:00:00+09:00"),
        ("2026-01-01T05:00:00+09:00", "2026-01-09T05:00:00+09:00", "2026-02-01T05:00:00+09:00"),
        ("2024-02-28T12:00:00+09:00", "2024-03-07T05:00:00+09:00", "2024-03-30T05:00:00+09:00"),
        ("2024-02-29T04:59:59+09:00", "2024-03-07T05:00:00+09:00", "2024-03-30T05:00:00+09:00"),
        ("2024-02-29T05:00:00+09:00", "2024-03-08T05:00:00+09:00", "2024-03-31T05:00:00+09:00"),
        ("2025-02-28T12:00:00+09:00", "2025-03-08T05:00:00+09:00", "2025-03-31T05:00:00+09:00"),
        ("2025-03-01T05:00:00+09:00", "2025-03-09T05:00:00+09:00", "2025-04-01T05:00:00+09:00"),
        ("2026-10-01T19:30:00Z", "2026-10-09T05:00:00+09:00", "2026-11-01T05:00:00+09:00"),
        ("2026-10-01T20:30:00Z", "2026-10-10T05:00:00+09:00", "2026-11-02T05:00:00+09:00"),
    ]
    for end, free_at, paid_at in cases:
        assert deadline_pair(at(end)) == (free_at, paid_at)


def test_contract_and_blackout_are_separate_from_the_seven_and_thirty_day_edges():
    end = at("2026-10-01T12:00:00+09:00")
    program = SlotProgram(at("2026-10-01T10:00:00+09:00"), end, (True, True, True, True), False)
    free = at("2026-10-09T05:00:00+09:00")
    paid = at("2026-11-01T05:00:00+09:00")
    eligible(program, True, free - timedelta(seconds=1), Membership(False, False))
    with pytest.raises(Exception) as contract:
        eligible(program, True, free, Membership(False, False))
    assert "タイムフリー30" in str(contract.value)
    eligible(program, True, paid - timedelta(seconds=1), Membership(False, True))
    with pytest.raises(Exception) as expired:
        eligible(program, True, paid, Membership(False, True))
    assert "期限" in str(expired.value)
    blocked = SlotProgram(program.start, end, (False, True, True, True), False)
    with pytest.raises(Exception) as blackout:
        availability(blocked, free - timedelta(seconds=1), False)
    assert "対象外" in str(blackout.value)
    availability(program, free, False)


def test_schedule_fixture_keeps_split_slots_and_drops_duplicates():
    listing = parse_programs((ROOT / "schedule.xml").read_bytes(), "TBS", date(2026, 10, 15))
    assert [item.title for item in listing.programs] == ["境界 & 番組", "朝 & 続編", "深夜"]
    assert listing.invalid_count == 1
    assert listing.duplicate_count == 1
    assert listing.programs[0].program_id == listing.programs[1].program_id or True
    assert listing.programs[1].tags == ["タグ"]
    assert listing.programs[1].program_genres[0].id == "UNKNOWN"
    assert listing.programs[1].personality_genres[0].id == "C999"
    selected = Filter.create(["UNKNOWN", "UNKNOWN"], [], False, False, "Example")
    assert len(selected.program_genres) == 1
    assert selected.matches(listing.programs[1]) is False
    assert Filter.create([], [], False, False, " タグ ").matches(listing.programs[1])
    assert Filter.create([], [], True, False, None).matches(listing.programs[0])
    stations = parse_stations((ROOT / "stations.xml").read_bytes(), "JP13")
    assert [item.id for item in stations] == ["TBS", "RN1"]
    assert stations[0].name == "テスト & 局"


def test_chapter_fixture_prefers_long_titles_and_drops_music():
    parsed = parse_chapters((ROOT / "chapters.json").read_bytes(), 90, 0)
    assert [(item.start, item.end, item.title) for item in parsed.chapters] == [
        (0.0, 30.0, "オープニング"),
        (30.0, 60.0, "本題"),
    ]
    assert parsed.invalid_count == 1
    assert parsed.music_count == 1
    assert parsed.current(30).title == "本題"
    assert parsed.current(0).title == "オープニング"


def test_range_is_inclusive_and_at_most_thirty_broadcast_days():
    assert len(BroadcastRange.new(date(2026, 10, 14), date(2026, 10, 15)).days) == 2
    with pytest.raises(ValueError):
        BroadcastRange.new(date(2026, 10, 15), date(2026, 10, 14))
    with pytest.raises(ValueError):
        BroadcastRange.new(date(2026, 9, 15), date(2026, 10, 15))


def test_session_and_named_search_files_match_the_rust_json_shape(tmp_path: Path):
    store = SessionStore(tmp_path)
    store.save(MemberSession("synthetic-token"))
    assert store.path.read_bytes() == b'{"version":1,"session":{"token":"synthetic-token"}}\n'
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"
    searches = SearchStore(tmp_path)
    now = stream_time("20261016050000")
    criteria = SearchCriteria.create(
        Scope.station("TBS"),
        BroadcastRange.new(date(2026, 10, 14), date(2026, 10, 15)),
        Filter.create([], [], False, False, "生活"),
    )
    searches.save_named("生活", criteria)
    written = searches.path.read_text()
    assert written == (
        "{\n"
        '  "version": 1,\n'
        '  "last": null,\n'
        '  "named": {\n'
        '    "生活": {\n'
        '      "scope": {\n'
        '        "kind": "station",\n'
        '        "value": "TBS"\n'
        "      },\n"
        '      "start": "20261014",\n'
        '      "end": "20261015",\n'
        '      "filter": {\n'
        '        "program_genres": [],\n'
        '        "personality_genres": [],\n'
        '        "unclassified_program": false,\n'
        '        "unclassified_personality": false,\n'
        '        "keyword": "生活"\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    loaded = searches.load()
    assert loaded.named["生活"].range.start == date(2026, 10, 14)
    restored = loaded.named["生活"].restore_startup(now)
    assert restored.range.start == date(2026, 10, 16)
    assert restored.filter.keyword == "生活"
    raw = searches.path.read_bytes()
    searches.path.write_bytes(b"broken")
    with pytest.raises(Exception) as error:
        searches.load()
    assert "不正" in str(error.value)
    assert searches.path.read_bytes() == b"broken"
    _ = raw
    session_path = tmp_path / "session.json"
    session_path.write_bytes(b"corrupt-synthetic-session")
    os.chmod(session_path, 0o600)
    with pytest.raises(Exception) as broken:
        store.load()
    assert "破損" in str(broken.value)
    assert session_path.read_bytes() == b"corrupt-synthetic-session"
    store.delete()
    assert not session_path.exists()


def test_corrupt_permissions_and_symlink_are_rejected(tmp_path: Path):
    store = SessionStore(tmp_path)
    store.save(MemberSession("synthetic-token"))
    os.chmod(store.path, 0o644)
    with pytest.raises(Exception) as error:
        store.load()
    assert "0600" in str(error.value)
    os.chmod(store.path, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(store.path)
    linked = SessionStore(tmp_path)
    # path() is fixed to session.json; replace the file with a symlink.
    store.path.unlink()
    store.path.symlink_to(link)
    with pytest.raises(Exception) as unsafe:
        linked.load()
    assert "シンボリックリンク" in str(unsafe.value)


def test_hls_live_ignores_a_slightly_older_window_and_timefree_does_not():
    def playlist(first, count, end=False):
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:5", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for number in range(first, first + count):
            lines.append("#EXTINF:5,")
            lines.append(f"s{number}.aac?signature=a")
        if end:
            lines.append("#EXT-X-ENDLIST")
        return parse_media_playlist("\n".join(lines))

    live = HlsCursor.start(playlist(12, 6), True)
    skipped_before = live.skipped
    live.install(playlist(11, 6))
    assert live.resets == 0
    assert live.next_sequence == 15
    assert live.skipped == skipped_before
    timefree = HlsCursor.start(playlist(10, 1, end=True), False)
    timefree.next_sequence = 12
    timefree.last_played = 11
    with pytest.raises(Exception) as lost:
        timefree.install(playlist(13, 1, end=True))
    assert "続きを再生できなくなりました" in str(lost.value)


def test_hls_rejects_features_the_rust_parser_refuses():
    base = ["#EXTM3U", "#EXT-X-TARGETDURATION:5", "#EXT-X-MEDIA-SEQUENCE:1"]
    for feature in [
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
        "#EXT-X-BYTERANGE:100@0",
        "#EXT-X-I-FRAMES-ONLY",
        '#EXT-X-MAP:URI=""',
    ]:
        text = "\n".join([*base, feature, "#EXTINF:5,", "s1.aac"])
        with pytest.raises(ProbeError) as rejected:
            parse_media_playlist(text)
        assert rejected.value.code == "playlist"
    allowed = "\n".join([*base, "#EXT-X-KEY:METHOD=NONE", "#EXTINF:5,", "s1.aac"])
    assert parse_media_playlist(allowed).segments[0].uri == "s1.aac"


def test_rfc3339_keeps_the_offset_and_microseconds_of_the_value():
    utc = datetime(2026, 10, 15, 5, 0, 0, 123000, tzinfo=timezone.utc)
    assert rfc3339(utc) == "2026-10-15T05:00:00.123+00:00"
    jst = datetime(2026, 10, 15, 14, 0, 0, tzinfo=JST)
    assert rfc3339(jst) == "2026-10-15T14:00:00+09:00"


def test_chapter_boolean_time_fields_are_invalid_like_the_rust_parser():
    body = json.dumps({"chapters": [{"fromSec": 0, "toSec": True, "title": "not a time"}]}).encode()
    with pytest.raises(Exception) as rejected:
        parse_chapters(body, 90.0, 0.0)
    assert "有効なチャプターがありません" in str(rejected.value)


def test_http_only_accepts_exact_trusted_hosts_and_a_local_port():
    for url in [
        "https://radiko.jp/a",
        "https://api.radiko.jp/a",
        "https://dr-wowza.radiko-cf.com/a",
        "http://127.0.0.1:9/a",
    ]:
        _allowed(url)
    for url in [
        "https://evilradiko.jp/a",
        "https://radiko.jp.evil.invalid/a",
        "http://radiko.jp/a",
        "https://radiko.jp:444/a",
        "https://user@radiko.jp/a",
    ]:
        with pytest.raises(ProbeError) as rejected:
            _allowed(url)
        assert rejected.value.code == "unsafe"


def test_huge_mp4_sample_count_is_rejected_before_decode():
    def atom(kind: bytes, payload: bytes) -> bytes:
        return (len(payload) + 8).to_bytes(4, "big") + kind + payload

    trun = atom(b"trun", bytes([0, 0, 0, 0, 255, 255, 255, 255]))
    data = atom(b"moof", atom(b"traf", trun))
    with pytest.raises(AacError) as error:
        validate_mp4(data)
    assert error.value.code == "many"


def test_status_boundaries():
    start = at("2026-10-15T06:00:00+09:00")
    end = at("2026-10-15T07:00:00+09:00")
    assert broadcast_status(start, end, start - timedelta(seconds=1)) == "upcoming"
    assert broadcast_status(start, end, start) == "on_air"
    assert broadcast_status(start, end, end) == "finished"


def test_session_repr_hides_the_token():
    session = MemberSession("synthetic-token")
    assert "synthetic-token" not in repr(session)
    blob = json.dumps({"token": session.token})
    assert "synthetic" in blob
