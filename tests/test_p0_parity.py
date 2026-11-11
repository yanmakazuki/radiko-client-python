"""P0（#15 配信解決の回復経路 / #16 fMP4 の境界）を固定する。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from radiko import prepare
from radiko.catalog import parse_endpoints
from radiko.domain import JST
from radiko.fragmented import (
    FragmentedAacDecoder,
    box,
    synthetic_fragment,
    synthetic_init,
    words,
)
from radiko.hls import parse_media_playlist
from radiko.media import AacError, encode_adts_sine, split_adts
from radiko.prepare import _apply_seek, _validate_seek_window, container_name
from radiko.storage import MemberSession, Membership
from radiko.stream import ProbeError

ASC = bytes([0x12, 0x08])
PACKETS = [frame.payload for frame in split_adts(encode_adts_sine(packets=3), lambda: False)]
INIT = synthetic_init(ASC, 44100, 1024, 0)
FRAGMENT_SECONDS = len(PACKETS) * 1024 / 44100
BASE = datetime(2026, 10, 15, 12, 0, 0, tzinfo=JST)


def _playlist(
    *segments: tuple[str, str | None, bool], end: bool = True, sequence: int = 0, map: bool = True
) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{sequence}"]
    if map:
        lines.append('#EXT-X-MAP:URI="init.mp4"')
    for uri, date, discontinuity in segments:
        if discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if date:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{date}")
        lines.append(f"#EXTINF:{FRAGMENT_SECONDS:.6f},")
        lines.append(uri)
    if end:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _program(start: datetime = BASE) -> SimpleNamespace:
    return SimpleNamespace(start=start, end=start + timedelta(hours=1), title="P0")


def _station_xml(*urls: str) -> bytes:
    rows = "".join(
        f'<url areafree="0" timefree="0"><playlist_create_url>{url}</playlist_create_url></url>'
        for url in urls
    )
    return f"<urls>{rows}</urls>".encode()


def test_endpoints_reject_an_untrusted_playlist_origin_before_fetching():
    data = _station_xml("https://evil.example/playlist.m3u8")
    with pytest.raises(ProbeError) as raised:
        parse_endpoints(data, False, False)
    assert raised.value.code == "unsafe"


def test_endpoints_keep_only_trusted_absolute_https_origins():
    data = _station_xml("https://example.radiko-cf.com/live/master.m3u8")
    assert parse_endpoints(data, False, False) == ["https://example.radiko-cf.com/live/master.m3u8"]


def test_container_name_rejects_a_non_synchsafe_id3_header():
    assert container_name(b"ID3\x03\x00\x00\x80\x00\x00\x00\xff\xf1") == "invalid ID3"


def test_container_name_skips_valid_id3_and_its_footer():
    tag = b"ID3\x04\x00\x10\x00\x00\x00\x00"
    assert container_name(tag + b"\x00" * 10 + b"\xff\xf1") == "MPEG-4 AAC ADTS"


def test_prepare_rejects_an_untrusted_endpoint_before_the_playlist(monkeypatch):
    program = prepare.ResolvedProgram(
        "P0", BASE, BASE + timedelta(hours=1), (True, True, True, True), False, None, None, "TBS"
    )
    fetched: list[str] = []

    def fake_get(url, headers=None, **kwargs):
        fetched.append(url)
        return SimpleNamespace(body=_station_xml("https://evil.example/playlist.m3u8"))

    monkeypatch.setattr(prepare, "fetch_program", lambda *args: program)
    monkeypatch.setattr(prepare, "authenticate_stream", lambda *args: ({}, "JP13"))
    monkeypatch.setattr(prepare, "_local_station", lambda *args: True)
    monkeypatch.setattr(prepare, "stream_get", fake_get)
    with pytest.raises(ProbeError) as raised:
        prepare.prepare(
            "TBS",
            "live",
            BASE + timedelta(minutes=1),
            BASE + timedelta(minutes=1),
            MemberSession("token"),
            Membership(None, None),
        )
    assert raised.value.stage == "配信先設定の取得"
    assert raised.value.inner is not None and raised.value.inner.code == "unsafe"
    assert fetched == ["https://radiko.jp/v3/station/stream/pc_html5/TBS.xml"]


def test_play_station_falls_back_after_a_server_error(monkeypatch):
    media = SimpleNamespace(
        media_sequence=0,
        segments=[],
        end_list=True,
        target_duration=2.0,
        discontinuity_sequence=0,
    )
    opened: list[str] = []

    def fake_open(url, headers, cancelled):
        opened.append(url)
        if url == "https://a.radiko-cf.com/one.m3u8":
            raise ProbeError("http", status=500)
        return url, media

    monkeypatch.setattr(
        prepare,
        "prepare",
        lambda *args: (
            _program(),
            {},
            "JP13",
            False,
            ["https://a.radiko-cf.com/one.m3u8", "https://b.radiko-cf.com/two.m3u8"],
        ),
    )
    monkeypatch.setattr(prepare, "_open_media", fake_open)
    report = prepare.play_station(
        "TBS",
        "live",
        datetime.now(JST),
        datetime.now(timezone.utc),
        MemberSession("token"),
        None,
        0.0,
        None,
        lambda: False,
    )
    assert report.reason == "program-end"
    assert opened == ["https://a.radiko-cf.com/one.m3u8", "https://b.radiko-cf.com/two.m3u8"]


def test_play_station_does_not_fall_back_after_authentication(monkeypatch):
    opened: list[str] = []

    def fake_open(url, headers, cancelled):
        opened.append(url)
        raise ProbeError("authentication")

    monkeypatch.setattr(
        prepare,
        "prepare",
        lambda *args: (_program(), {}, "JP13", False, ["https://a.radiko-cf.com/one.m3u8"]),
    )
    monkeypatch.setattr(prepare, "_open_media", fake_open)
    with pytest.raises(ProbeError) as raised:
        prepare.play_station(
            "TBS",
            "live",
            datetime.now(JST),
            datetime.now(timezone.utc),
            MemberSession("token"),
            None,
            0.0,
            None,
            lambda: False,
        )
    assert raised.value.code == "authentication"
    assert opened == ["https://a.radiko-cf.com/one.m3u8"]


def test_seek_validation_matches_the_program_date_time_window():
    media = parse_media_playlist(
        _playlist(("one.aac", "2026-10-15T12:00:03+09:00", False), end=False)
    )
    with pytest.raises(ProbeError) as raised:
        _validate_seek_window(media, _program(), 0.0)
    assert "指定位置より後の配信です" in str(raised.value)
    with pytest.raises(ProbeError) as raised:
        _validate_seek_window(media, _program(), 30.0)
    assert "シーク先に対応しない配信です" in str(raised.value)
    _validate_seek_window(media, _program(), 3.0)


def test_apply_seek_restarts_when_the_target_is_outside_the_window():
    media = parse_media_playlist(
        _playlist(
            ("one.aac", "2026-10-15T12:00:00+09:00", False),
            ("two.aac", "2026-10-15T12:00:02+09:00", False),
            end=False,
        )
    )
    control = SimpleNamespace(accepted=[500.0], applied=0)
    sequence, _seconds, _pending, restart = _apply_seek(control, media, 0, 0.0, BASE, None)
    assert restart == 500.0
    assert sequence == 0
    control = SimpleNamespace(accepted=[1.0], applied=0)
    sequence, seconds, pending, restart = _apply_seek(control, media, 0, 0.0, BASE, None)
    assert restart is None
    assert (sequence, seconds) == (1, 1.0)
    assert pending is not None


def test_apply_seek_restarts_for_a_rewind_before_the_window():
    media = parse_media_playlist(
        _playlist(("one.aac", "2026-10-15T12:00:05+09:00", False), end=False)
    )
    control = SimpleNamespace(accepted=[0.0], applied=0)
    _sequence, _seconds, _pending, restart = _apply_seek(control, media, 5, 5.0, BASE, None)
    assert restart == 0.0


def test_fragmented_decoder_rejects_a_bad_tkhd_version():
    broken = INIT.replace(b"tkhd", b"tkhd", 1)
    at = broken.find(b"tkhd") + 4
    broken = broken[:at] + b"\x02" + broken[at + 1 :]
    with pytest.raises(AacError) as raised:
        FragmentedAacDecoder(broken)
    assert raised.value.code == "container"


def _insert_dinf(init: bytes) -> bytes:
    from radiko.fragmented import _atoms

    top = _atoms(init)
    moov = _atoms(top[1].data)
    trak = _atoms(moov[0].data)
    mdia = _atoms(trak[1].data)
    minf = _atoms(mdia[1].data)
    dref = box(b"dref", words(0, 1) + box(b"url ", words(2)))
    rebuilt = b"".join(box(item.kind, bytes(item.data)) for item in minf) + box(b"dinf", dref)
    new_minf = box(b"minf", rebuilt)
    new_mdia = box(b"mdia", box(b"mdhd", bytes(mdia[0].data)) + new_minf)
    new_trak = box(b"trak", box(b"tkhd", bytes(trak[0].data)) + new_mdia)
    new_moov = box(b"moov", new_trak + box(b"mvex", bytes(moov[1].data)))
    return box(b"ftyp", bytes(top[0].data)) + new_moov


def test_fragmented_decoder_rejects_an_external_dref():
    broken = _insert_dinf(INIT)
    with pytest.raises(AacError) as raised:
        FragmentedAacDecoder(broken)
    assert raised.value.code == "unsupported-container"


def test_fragmented_decoder_rejects_declared_he_aac_without_sbr():
    decoder = FragmentedAacDecoder(INIT)
    with pytest.raises(AacError) as raised:
        decoder.decode_pcm(synthetic_fragment(PACKETS, 0, 1), None, "mp4a.40.5")
    assert raised.value.code == "sbr-missing"


class _State:
    def __init__(self):
        self.renewed: list[str] = []
        self.headers = {"x-radiko-authtoken": "old"}

    def renew(self, headers, stage):
        self.renewed.append(stage)
        self.headers = {"x-radiko-authtoken": "new"}
        return self.headers, "refreshed"


def test_playlist_refresh_reauthenticates_on_401(monkeypatch):
    calls: list[str] = []

    def fake_get(url, headers=None, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise ProbeError("authentication")
        return _Reply(_playlist(("one.aac", None, False)).encode(), url)

    monkeypatch.setattr(prepare, "stream_get", fake_get)
    state = _State()
    refreshed, url, headers = prepare._refresh_playlist(
        "https://e.radiko-cf.com/media.m3u8",
        "https://e.radiko-cf.com/source.m3u8",
        state.headers,
        lambda: False,
        state,
        "timefree",
        True,
        "HLS一覧更新",
    )
    assert [segment.uri for segment in refreshed.segments] == ["one.aac"]
    assert url == "https://e.radiko-cf.com/media.m3u8"
    assert headers["x-radiko-authtoken"] == "new"
    assert state.renewed == ["HLS一覧更新"]


def test_live_expired_chunklist_is_rebuilt_from_the_source_url(monkeypatch):
    media = parse_media_playlist(_playlist(("new.aac", None, False)))
    opened: list[str] = []

    def fake_open(url, headers, cancelled):
        opened.append(url)
        return "https://e.radiko-cf.com/new.m3u8", media

    def fake_get(url, headers=None, **kwargs):
        raise ProbeError("not-found")

    monkeypatch.setattr(prepare, "stream_get", fake_get)
    monkeypatch.setattr(prepare, "_open_media", fake_open)
    state = _State()
    refreshed, url, _headers = prepare._refresh_playlist(
        "https://e.radiko-cf.com/expired.m3u8",
        "https://e.radiko-cf.com/source.m3u8",
        state.headers,
        lambda: False,
        state,
        "live",
        True,
        "HLS一覧更新",
    )
    assert [segment.uri for segment in refreshed.segments] == ["new.aac"]
    assert url == "https://e.radiko-cf.com/new.m3u8"
    assert opened == ["https://e.radiko-cf.com/source.m3u8"]
    assert state.renewed == []


def test_interactive_seek_outside_the_window_reprepares_with_seek(monkeypatch):
    initial = _playlist(("one.m4s", "2026-10-15T12:00:00+09:00", False))
    later = _playlist(("one.m4s", "2026-10-15T12:01:40+09:00", False))
    fragment = synthetic_fragment(PACKETS, 0, 1)
    seen: list[float] = []

    def fake_prepare(station, mode, cursor, now, session, membership, position, cancelled):
        seen.append(position)
        return _program(), {}, "JP13", False, [f"https://e.radiko-cf.com/p{position:.1f}.m3u8"]

    def fake_get(url, headers=None, **kwargs):
        if url.endswith("p0.0.m3u8"):
            return _Reply(initial.encode(), url)
        if url.endswith("p100.0.m3u8"):
            return _Reply(later.encode(), url)
        if url.endswith("init.mp4"):
            return _Reply(INIT, url)
        if url.endswith("one.m4s"):
            return _Reply(fragment, url)
        raise AssertionError(url)

    monkeypatch.setenv("RADIKO_NULL_SINK", "1")
    monkeypatch.setattr(prepare, "prepare", fake_prepare)
    monkeypatch.setattr(prepare, "stream_get", fake_get)
    report = prepare.play_station(
        "TBS",
        "timefree",
        datetime.now(JST),
        datetime.now(timezone.utc),
        MemberSession("token"),
        None,
        0.0,
        None,
        lambda: False,
        commands=["seek 100"],
    )
    assert seen == [0.0, 100.0]
    assert report.reason == "program-end"
    assert report.segments == 1


class _Reply:
    def __init__(self, body: bytes, url: str) -> None:
        self.body = body
        self.url = url


def test_discontinuity_resets_the_fragment_decoder_and_the_clock(monkeypatch):
    first = synthetic_fragment(PACKETS, 0, 1)
    second = synthetic_fragment(PACKETS, 0, 99)
    text = _playlist(
        ("one.m4s", "2026-10-15T12:00:00+09:00", False),
        ("two.m4s", "2026-10-15T13:00:00+09:00", True),
    )

    def fake_get(url, headers=None, **kwargs):
        if url.endswith("media.m3u8"):
            return _Reply(text.encode(), "https://example.radiko-cf.com/media.m3u8")
        if url.endswith("init.mp4"):
            return _Reply(INIT, url)
        if url.endswith("one.m4s"):
            return _Reply(first, url)
        if url.endswith("two.m4s"):
            return _Reply(second, url)
        raise AssertionError(url)

    monkeypatch.setenv("RADIKO_NULL_SINK", "1")
    monkeypatch.setattr(
        prepare,
        "prepare",
        lambda *args: (_program(), {}, "JP13", False, ["https://example.radiko-cf.com/media.m3u8"]),
    )
    monkeypatch.setattr(prepare, "stream_get", fake_get)
    report = prepare.play_station(
        "TBS",
        "timefree",
        datetime.now(JST),
        datetime.now(timezone.utc),
        MemberSession("token"),
        None,
        0.0,
        None,
        lambda: False,
    )
    assert report.reason == "program-end"
    assert report.segments == 2
    assert report.frames == 2 * len(PACKETS) * 1024


ADTS2 = encode_adts_sine(packets=2)
ADTS3 = encode_adts_sine(packets=3)
ADTS2_FRAMES = len(split_adts(ADTS2, lambda: False)) * 1024
ADTS3_FRAMES = len(split_adts(ADTS3, lambda: False)) * 1024


def test_timefree_failure_after_playback_neither_reauths_nor_reopens_source(monkeypatch):
    opened: list[str] = []
    for code in ("denied", "not-found"):
        state = _State()

        def fake_get(url, headers=None, **kwargs):
            raise ProbeError(code)

        monkeypatch.setattr(prepare, "stream_get", fake_get)
        monkeypatch.setattr(
            prepare,
            "_open_media",
            lambda url, headers, cancelled: opened.append(url),
        )
        with pytest.raises(ProbeError) as raised:
            prepare._refresh_playlist(
                "https://e.radiko-cf.com/media.m3u8",
                "https://e.radiko-cf.com/source.m3u8",
                state.headers,
                lambda: False,
                state,
                "timefree",
                True,
                "HLS一覧更新",
            )
        assert raised.value.code == code
        assert state.renewed == []
        assert opened == []


def test_live_source_is_renewed_once_when_it_answers_401(monkeypatch):
    media = parse_media_playlist(_playlist(("new.aac", None, False)))
    attempts = {"open": 0}

    def fake_open(url, headers, cancelled):
        attempts["open"] += 1
        if attempts["open"] == 1:
            raise ProbeError("authentication")
        return "https://e.radiko-cf.com/new.m3u8", media

    def fake_get(url, headers=None, **kwargs):
        raise ProbeError("not-found")

    monkeypatch.setattr(prepare, "stream_get", fake_get)
    monkeypatch.setattr(prepare, "_open_media", fake_open)
    state = _State()
    refreshed, url, _headers = prepare._refresh_playlist(
        "https://e.radiko-cf.com/expired.m3u8",
        "https://e.radiko-cf.com/source.m3u8",
        state.headers,
        lambda: False,
        state,
        "live",
        True,
        "HLS一覧更新",
    )
    assert [segment.uri for segment in refreshed.segments] == ["new.aac"]
    assert url == "https://e.radiko-cf.com/new.m3u8"
    assert state.renewed == ["HLS一覧更新"]
    assert attempts["open"] == 2


def test_live_window_jump_asks_for_a_decoder_reset():
    media = parse_media_playlist(_playlist(("a.aac", None, False), end=False))
    jumped = parse_media_playlist(
        _playlist(("a.aac", None, False), ("b.aac", None, False), end=False, sequence=10)
    )
    _m, _u, _s, _h, reset = prepare._apply_playlist(
        jumped, "https://e.radiko-cf.com/media.m3u8", media, 0, "live", None, {}
    )
    assert reset is True
    same = parse_media_playlist(_playlist(("a.aac", None, False), end=False))
    _m, _u, _s, _h, reset = prepare._apply_playlist(
        same, "https://e.radiko-cf.com/media.m3u8", media, 0, "timefree", None, {}
    )
    assert reset is False


def test_probe_fills_the_aac_core_fields_for_fragmented_mp4(monkeypatch):
    fragment = synthetic_fragment(PACKETS, 0, 1)
    text = _playlist(("one.m4s", "2026-10-15T12:00:00+09:00", False))

    def fake_get(url, headers=None, **kwargs):
        if url.endswith("media.m3u8"):
            return _Reply(text.encode(), url)
        if url.endswith("init.mp4"):
            return _Reply(INIT, url)
        if url.endswith("one.m4s"):
            return _Reply(fragment, url)
        raise AssertionError(url)

    monkeypatch.setattr(prepare, "stream_get", fake_get)
    report = prepare.inspect_hls("https://e.radiko-cf.com/media.m3u8", {}, False, lambda: False)
    assert report.container == "ISO BMFF / fragmented MP4"
    assert report.decode_error is None
    assert report.packets == len(PACKETS)
    assert report.frames == len(PACKETS) * 1024
    assert report.core_sample_rate == 44100
    assert report.core_channels == 1
    assert report.sample_rate == 44100
    assert report.sbr_detected is False
    assert report.stereo_expansion is False
    assert report.nonzero_samples > 0
    assert report.decode_duration > 0


def test_window_out_seek_keeps_volume_and_position(monkeypatch):
    # セグメントの取得回数で命令を渡す。emit の step_hook も drain を呼ぶため。
    pending: list[tuple[str, int]] = [
        ("volume 80", 0),
        ("seek 100", 1),
        ("status", 2),
        ("+15", 2),
    ]
    fetched = {"n": 0}
    seen: list[float] = []
    notices: list[str] = []

    def fake_prepare(station, mode, cursor, now, session, membership, position, cancelled):
        seen.append(position)
        return _program(), {}, "JP13", False, [f"https://e.radiko-cf.com/p{position:.1f}.m3u8"]

    def fake_get(url, headers=None, **kwargs):
        if ".m3u8" in url:
            position = float(url.rsplit("p", 1)[1].split(".m3u8")[0])
            stamp = (BASE + timedelta(seconds=position)).strftime("%Y-%m-%dT%H:%M:%S+09:00")
            return _Reply(_playlist(("one.aac", stamp, False), map=False).encode(), url)
        if url.endswith("one.aac"):
            fetched["n"] += 1
            return _Reply(ADTS3, url)
        raise AssertionError(url)

    def commands():
        if pending and fetched["n"] >= pending[0][1]:
            return pending.pop(0)[0]
        return None

    monkeypatch.setenv("RADIKO_NULL_SINK", "1")
    monkeypatch.setattr(prepare, "prepare", fake_prepare)
    monkeypatch.setattr(prepare, "stream_get", fake_get)
    report = prepare.play_station(
        "TBS",
        "timefree",
        datetime.now(JST),
        datetime.now(timezone.utc),
        MemberSession("token"),
        None,
        0.0,
        None,
        lambda: False,
        commands=commands,
        on_notice=notices.append,
    )
    assert seen == [0.0, 100.0, 115.0]
    assert report.reason == "program-end"
    assert report.segments == 3
    assert any("音量80%" in message and "100.000秒" in message for message in notices)


def test_station_switch_reports_only_the_new_station(monkeypatch):
    def fake_prepare(station, mode, cursor, now, session, membership, position, cancelled):
        program = _program()
        program.title = f"{station}番組"
        return program, {}, "JP13", False, [f"https://e.radiko-cf.com/{station}.m3u8"]

    def fake_get(url, headers=None, **kwargs):
        if url.endswith("/TBS.m3u8"):
            body = _playlist(("a.aac", "2026-10-15T12:00:00+09:00", False), map=False)
            return _Reply(body.encode(), url)
        if url.endswith("/QRR.m3u8"):
            body = _playlist(("b.aac", "2026-10-15T12:00:00+09:00", False), map=False)
            return _Reply(body.encode(), url)
        if url.endswith("a.aac"):
            return _Reply(ADTS2, url)
        if url.endswith("b.aac"):
            return _Reply(ADTS3, url)
        raise AssertionError(url)

    calls = {"n": 0}

    def commands():
        calls["n"] += 1
        return "live QRR" if calls["n"] == 2 else None

    monkeypatch.setenv("RADIKO_NULL_SINK", "1")
    monkeypatch.setattr(prepare, "prepare", fake_prepare)
    monkeypatch.setattr(prepare, "stream_get", fake_get)
    report = prepare.play_station(
        "TBS",
        "timefree",
        datetime.now(JST),
        datetime.now(timezone.utc),
        MemberSession("token"),
        None,
        0.0,
        None,
        lambda: False,
        commands=commands,
    )
    assert report.station == "QRR"
    assert report.mode == "Live"
    assert report.segments == 1
    assert report.frames == ADTS3_FRAMES
    assert report.frames != ADTS2_FRAMES + ADTS3_FRAMES
