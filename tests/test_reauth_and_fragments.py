"""ローカルサーバが本物の箱と 401 を返す。成功を関数の戻り値で偽装しない。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from radiko.domain import JST
from radiko.fragmented import synthetic_fragment, synthetic_init
from radiko.media import encode_adts_sine, split_adts
from radiko.storage import MemberSession, SessionStore
from radiko_client.cli import main

UTC = timezone.utc
ASC = bytes([0x12, 0x08])
PACKETS = [frame.payload for frame in split_adts(encode_adts_sine(packets=3), lambda: False)]
INIT = synthetic_init(ASC, 44100, 1024, 0)
FRAGMENTS = [
    synthetic_fragment(PACKETS, 0, 1),
    synthetic_fragment(PACKETS, 1024 * len(PACKETS), 2),
]
FRAGMENT_SECONDS = len(PACKETS) * 1024 / 44100
ADTS = encode_adts_sine(packets=3)
STATIONS = open("tests/fixtures/stations.xml", "rb").read()
STREAMS = open("tests/fixtures/streams.xml", "rb").read()


def _program(when: datetime) -> bytes:
    start = when - timedelta(minutes=30)
    end = when + timedelta(hours=2)
    return json.dumps(
        {
            "programs": [
                {
                    "stationId": "TBS",
                    "title": "箱番組",
                    "startAt": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endAt": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "blackout": {
                        "tsInNg": 0,
                        "tsplusInNg": 0,
                        "tsOutNg": 0,
                        "tsplusOutNg": 0,
                        "failedRecord": 0,
                    },
                }
            ]
        }
    ).encode()


class _State:
    def __init__(self):
        self.paths: list[str] = []
        self.auth1 = 0
        self.mode = "fragments"
        self.cancel = threading.Event()
        self.forbid = False
        self.limit = 0
        self.failures = 0


class _Handler(BaseHTTPRequestHandler):
    state: _State = _State()

    def do_GET(self):
        state = _Handler.state
        parsed = urlsplit(self.path)
        state.paths.append(parsed.path)
        token = self.headers.get("x-radiko-authtoken")
        if parsed.path == "/v2/api/auth1":
            state.auth1 += 1
            if state.auth1 >= 2 and state.cancel.is_set():
                state.cancel.set()
            token_value = "stream-token" if state.auth1 == 1 else "renewed-token"
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-radiko-authtoken", token_value)
            self.send_header("x-radiko-keyoffset", "0")
            self.send_header("x-radiko-keylength", "4")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/v2/api/auth2":
            body = b"JP13,Tokyo\n"
        elif parsed.path == "/ap/member/webapi/v2/member/login/check":
            body = b'{"status":"200","areafree":1,"timefreeplus":1}'
        elif parsed.path == "/v1/programs/list":
            when = datetime.fromisoformat(
                parse_qs(parsed.query)["lastStartAt"][0].replace("Z", "+00:00")
            )
            body = _program(when - timedelta(seconds=1))
        elif parsed.path == "/v3/station/list/JP13.xml":
            body = STATIONS
        elif parsed.path == "/v3/station/stream/pc_html5/TBS.xml":
            body = STREAMS
        elif parsed.path.endswith("/master.m3u8"):
            media = (
                "/frag/media.m3u8"
                if state.mode in {"fragments", "skew"}
                else (
                    "/out/media.m3u8"
                    if state.mode in {"outage", "startup408", "http408", "drop"}
                    else "/auth/media.m3u8"
                )
            )
            body = (
                "#EXTM3U\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=48000,CODECS="mp4a.40.2",AUDIO="main"\n'
                "unused.m3u8\n"
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="main",DEFAULT=YES,URI="{media}"\n'
            ).encode()
        elif parsed.path == "/frag/media.m3u8":
            gap = "2026-10-15T12:00:10+09:00" if state.mode == "skew" else ""
            second_date = f"#EXT-X-PROGRAM-DATE-TIME:{gap}\n" if gap else ""
            body = (
                "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n"
                '#EXT-X-MAP:URI="/frag/init.mp4"\n'
                "#EXT-X-PROGRAM-DATE-TIME:2026-10-15T12:00:00+09:00\n"
                f"#EXTINF:{FRAGMENT_SECONDS:.6f},\n/frag/1.m4s\n"
                f"{second_date}#EXTINF:{FRAGMENT_SECONDS:.6f},\n/frag/2.m4s\n"
                "#EXT-X-ENDLIST\n"
            ).encode()
        elif parsed.path == "/out/media.m3u8":
            body = (
                b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n"
                b"#EXTINF:1.0,\n/out/a.aac\n#EXTINF:1.0,\n/out/b.aac\n#EXT-X-ENDLIST\n"
            )
        elif parsed.path == "/out/a.aac":
            if state.mode == "startup408":
                state.failures = getattr(state, "failures", 0) + 1
                self.send_response(408)
                self.end_headers()
                return
            body = ADTS
        elif parsed.path == "/out/b.aac":
            state.failures = getattr(state, "failures", 0) + 1
            if state.mode == "startup408":
                body = ADTS
            elif state.mode == "drop" and state.failures <= state.limit:
                self.close_connection = True
                return
            elif state.mode == "http408" and state.failures <= state.limit:
                self.send_response(408)
                self.end_headers()
                return
            elif state.mode == "outage" and state.failures <= 8:
                self.send_response(500)
                self.end_headers()
                return
            else:
                body = ADTS
        elif parsed.path == "/frag/init.mp4":
            body = INIT
        elif parsed.path == "/frag/1.m4s":
            body = FRAGMENTS[0]
        elif parsed.path == "/frag/2.m4s":
            body = FRAGMENTS[1]
        elif parsed.path == "/auth/media.m3u8":
            if token == "renewed-token" and state.mode == "window":
                body = (
                    b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:30\n"
                    b"#EXTINF:1.0,\n/auth/new.aac\n#EXT-X-ENDLIST\n"
                )
            elif token == "renewed-token" and state.mode == "livejump":
                body = (
                    b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:5\n"
                    b"#EXTINF:1.0,\n/auth/jump.aac\n#EXT-X-ENDLIST\n"
                )
            elif token == "renewed-token":
                body = (
                    b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n"
                    b"#EXTINF:1.0,\n/auth/new.aac\n#EXT-X-ENDLIST\n"
                )
            else:
                body = (
                    b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n"
                    b"#EXTINF:1.0,\n/auth/old.aac\n#EXT-X-ENDLIST\n"
                )
        elif parsed.path == "/auth/old.aac":
            if state.forbid:
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(401)
            self.end_headers()
            return
        elif parsed.path == "/auth/jump.aac":
            body = ADTS
        elif parsed.path == "/auth/new.aac":
            if state.mode == "second401":
                self.send_response(401)
                self.end_headers()
                return
            body = ADTS
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _serve(mode: str):
    state = _State()
    state.mode = mode
    _Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def _env(monkeypatch, server, data):
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("RADIKO_LOOPBACK_ORIGIN", origin)
    monkeypatch.setenv("RADIKO_AUTH_ORIGIN", origin)
    monkeypatch.setenv("RADIKO_NULL_SINK", "1")
    SessionStore(data).save(MemberSession("synthetic-token"))


def _yesterday() -> str:
    return (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d120000")


def test_program_date_skew_rejects_the_fragment(tmp_path, monkeypatch, capsys):
    server, _state = _serve("skew")
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(
            ["--data-dir", str(tmp_path), "play-timefree", "TBS", _yesterday(), "--seconds", "30"]
        )
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 1
    assert "コンテナ" in err
    assert "番組終了" not in out


def _run(server, data_dir, monkeypatch, **kwargs):
    _env(monkeypatch, server, data_dir)
    from radiko.auth import MemberClient
    from radiko.prepare import play_station

    session = MemberSession("synthetic-token")
    membership = MemberClient.create().check(session)
    when = datetime.strptime(_yesterday(), "%Y%m%d%H%M%S").replace(tzinfo=JST)
    return play_station(
        "TBS",
        "timefree",
        when,
        datetime.now(UTC),
        session,
        membership,
        0.0,
        30.0,
        kwargs.pop("cancelled", lambda: False),
        **kwargs,
    )


def test_outage_retries_on_a_virtual_clock_then_plays(tmp_path, monkeypatch, capsys):
    server, state = _serve("outage")
    slept: list[float] = []
    delays: list[float] = []
    try:
        report = _run(
            server,
            tmp_path,
            monkeypatch,
            reconnect_sleep=slept.append,
            transport_sleep=lambda _seconds: None,
            transport_delays=delays,
        )
    finally:
        server.shutdown()
    assert report.reason == "program-end", report
    assert abs(sum(slept) - 3.0) < 1e-4
    assert state.failures == 9
    assert len(delays) == 6
    assert state.paths.count("/out/media.m3u8") >= 3


def test_startup_408_stops_before_the_reconnect_budget(tmp_path, monkeypatch):
    server, state = _serve("startup408")
    delays: list[float] = []
    slept: list[float] = []
    from radiko.stream import ProbeError

    try:
        with pytest.raises(ProbeError) as raised:
            _run(
                server,
                tmp_path,
                monkeypatch,
                reconnect_sleep=slept.append,
                transport_sleep=lambda _seconds: None,
                transport_delays=delays,
            )
    finally:
        server.shutdown()
    assert raised.value.status == 408
    assert state.failures == 4
    assert slept == []
    assert all(delay >= base for delay, base in zip(delays, (0.1, 0.2, 0.4), strict=True))
    assert all(delay < base + 0.05 for delay, base in zip(delays, (0.1, 0.2, 0.4), strict=True))
    assert "/out/b.aac" not in state.paths


def test_408_after_audio_keeps_the_same_segment_and_waits_one_second(tmp_path, monkeypatch):
    server, state = _serve("http408")
    state.limit = 4
    slept: list[float] = []
    delays: list[float] = []
    try:
        report = _run(
            server,
            tmp_path,
            monkeypatch,
            reconnect_sleep=slept.append,
            transport_sleep=lambda _seconds: None,
            transport_delays=delays,
        )
    finally:
        server.shutdown()
    assert report.reason == "program-end", report
    assert state.paths.count("/out/b.aac") == 5
    assert abs(sum(slept) - 1.0) < 1e-4
    assert len(delays) == 3
    assert state.paths.count("/out/media.m3u8") >= 2


def test_dropped_connection_uses_the_same_retry_sequence(tmp_path, monkeypatch):
    server, state = _serve("drop")
    state.limit = 1
    slept: list[float] = []
    delays: list[float] = []
    try:
        report = _run(
            server,
            tmp_path,
            monkeypatch,
            reconnect_sleep=slept.append,
            transport_sleep=lambda _seconds: None,
            transport_delays=delays,
        )
    finally:
        server.shutdown()
    assert report.reason == "program-end", report
    assert state.paths.count("/out/b.aac") == 2
    assert slept == []
    assert len(delays) == 1
    assert 0.1 <= delays[0] < 0.15


def test_cancel_during_408_backoff_does_not_fetch_again(tmp_path, monkeypatch):
    server, state = _serve("http408")
    state.limit = 8
    gate = {"stop": False}
    try:
        report = _run(
            server,
            tmp_path,
            monkeypatch,
            cancelled=lambda: gate["stop"],
            reconnect_sleep=lambda _seconds: None,
            transport_sleep=lambda _seconds: gate.__setitem__("stop", True),
        )
    finally:
        server.shutdown()
    assert report.reason == "cancelled"
    assert report.frames > 0
    assert state.paths.count("/out/b.aac") == 1
    assert state.paths.count("/out/a.aac") == 1


def test_two_fragments_play_through_the_real_boxes(tmp_path, monkeypatch, capsys):
    server, state = _serve("fragments")
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(
            ["--data-dir", str(tmp_path), "play-timefree", "TBS", _yesterday(), "--seconds", "30"]
        )
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "2セグメント" in out
    assert f"{1024 * len(PACKETS) * 2}フレーム" in out
    assert state.paths.count("/frag/1.m4s") == 1
    assert state.paths.count("/frag/2.m4s") == 1
    assert "/frag/init.mp4" in state.paths
    assert "synthetic-token" not in out + err


def test_segment_401_reauthenticates_once_and_plays_the_new_bytes(tmp_path, monkeypatch, capsys):
    server, state = _serve("recover")
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(
            ["--data-dir", str(tmp_path), "play-timefree", "TBS", _yesterday(), "--seconds", "30"]
        )
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "番組終了" in out
    assert state.auth1 == 2
    assert "/auth/old.aac" in state.paths
    assert "/auth/new.aac" in state.paths
    assert "renewed-token" not in out + err
    assert "synthetic-token" not in out + err


def test_second_401_stops_without_a_third_authenticate(tmp_path, monkeypatch, capsys):
    server, state = _serve("second401")
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(
            ["--data-dir", str(tmp_path), "play-timefree", "TBS", _yesterday(), "--seconds", "30"]
        )
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 1
    assert "認証期限切れ" in err
    assert "番組終了" not in out
    assert state.auth1 == 2


def test_forbidden_does_not_reauthenticate(tmp_path, monkeypatch, capsys):
    server, state = _serve("recover")
    state.forbid = True
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(
            ["--data-dir", str(tmp_path), "play-timefree", "TBS", _yesterday(), "--seconds", "30"]
        )
    finally:
        server.shutdown()
    _out, err = capsys.readouterr()
    assert code == 1
    assert "アクセスを拒否しました" in err
    assert state.auth1 == 1


def test_cancel_during_reauthentication_does_not_emit_audio(tmp_path, monkeypatch):
    server, state = _serve("recover")
    _env(monkeypatch, server, tmp_path)
    from radiko.auth import MemberClient
    from radiko.prepare import play_station

    session = MemberSession("synthetic-token")
    membership = MemberClient.create().check(session)

    def cancelled():
        return state.auth1 >= 2

    when = datetime.strptime(_yesterday(), "%Y%m%d%H%M%S").replace(tzinfo=JST)
    try:
        report = play_station(
            "TBS", "timefree", when, datetime.now(UTC), session, membership, 0.0, 30.0, cancelled
        )
    finally:
        server.shutdown()
    assert report.reason == "cancelled"
    assert report.frames == 0
    assert "/auth/new.aac" not in state.paths
    assert "synthetic-token" not in repr(report)


def test_live_401_catch_up_fetches_the_later_fragment(tmp_path, monkeypatch, capsys):
    server, state = _serve("livejump")
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(["--data-dir", str(tmp_path), "play-live", "TBS", "--seconds", "30"])
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "/auth/jump.aac" in state.paths
    assert state.auth1 == 2
    assert "renewed-token" not in out + err


def test_timefree_401_with_a_jumped_playlist_is_window_lost(tmp_path, monkeypatch, capsys):
    server, state = _serve("window")
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(
            ["--data-dir", str(tmp_path), "play-timefree", "TBS", _yesterday(), "--seconds", "30"]
        )
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 1
    assert "続きを再生できなくなりました" in err
    assert "番組終了" not in out
    assert state.auth1 == 2
    assert "/auth/new.aac" not in state.paths
