"""本番と同じパスを 127.0.0.1 へ載せて、probe / chapters / play を通す。

null sink はフレーム数だけ進む単体出力であり、壁時計の再生時間ではない。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from radiko.domain import JST
from radiko.media import encode_adts_sine, split_adts
from radiko.storage import MemberSession, SessionStore
from radiko.urls import append_query, transport_url, trusted
from radiko_client.cli import main

UTC = timezone.utc
ADTS = encode_adts_sine(packets=50)
PACKETS = len(split_adts(ADTS, lambda: False))
STREAMS = (Path(__file__).parent / "fixtures" / "streams.xml").read_bytes()
STATIONS = (Path(__file__).parent / "fixtures" / "stations.xml").read_bytes()


def _program(when: datetime, *, event: bool) -> bytes:
    start = when - timedelta(minutes=30)
    end = when + timedelta(hours=2)
    body = {
        "programs": [
            {
                "stationId": "TBS",
                "title": "合成番組",
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
    if event:
        body["programs"][0]["eventId"] = "SYNTHETIC-2"
        body["programs"][0]["episodeIntroductionStartAt"] = body["programs"][0]["startAt"]
    return json.dumps(body).encode()


CHAPTERS = json.dumps(
    {
        "stationId": "TBS",
        "programId": "SYNTHETIC-2",
        "chapters": [{"title": "本題", "fromSec": 10, "toSec": 20}],
    }
).encode()

MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=48000,CODECS="mp4a.40.2",AUDIO="main"\n'
    "unused.m3u8\n"
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="main",DEFAULT=YES,AUTOSELECT=YES,URI="/media.m3u8"\n'
).encode()
MEDIA = (
    "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n"
    "#EXTINF:1.5,\n/seg.aac\n#EXT-X-ENDLIST\n"
).encode()


class _Handler(BaseHTTPRequestHandler):
    paths: list[str] = []
    fail_auth = False
    cancel_on_segment = False
    stop: threading.Event | None = None
    origin_stamp: str | None = None
    login_body = b""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _Handler.paths.append(urlsplit(self.path).path)
        if urlsplit(self.path).path == "/ap/member/webapi/member/login":
            _Handler.login_body = body
            payload = b'{"radiko_session":"login-token"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urlsplit(self.path)
        _Handler.paths.append(parsed.path)
        if _Handler.cancel_on_segment and parsed.path == "/seg.aac" and _Handler.stop is not None:
            _Handler.stop.set()
        if parsed.path == "/ap/member/webapi/v2/member/login/check":
            body = b'{"status":"200","areafree":1,"timefreeplus":1}'
        elif parsed.path == "/v2/api/auth1":
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-radiko-authtoken", "stream-token")
            self.send_header("x-radiko-keyoffset", "0")
            self.send_header("x-radiko-keylength", "4")
            self.end_headers()
            self.wfile.write(body)
            return
        elif parsed.path == "/v2/api/auth2":
            if _Handler.fail_auth:
                self.send_response(401)
                self.end_headers()
                return
            body = b"JP13,Tokyo\n"
        elif parsed.path == "/v1/programs/list":
            query = parse_qs(parsed.query)
            when = datetime.fromisoformat(query["lastStartAt"][0].replace("Z", "+00:00"))
            body = _program(when - timedelta(seconds=1), event=True)
        elif parsed.path == "/v3/station/list/JP13.xml":
            body = STATIONS
        elif parsed.path == "/v3/station/stream/pc_html5/TBS.xml":
            body = STREAMS
        elif parsed.path.endswith("/master.m3u8"):
            body = MASTER
        elif parsed.path == "/media.m3u8":
            if _Handler.origin_stamp:
                body = (
                    "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n"
                    f"#EXT-X-PROGRAM-DATE-TIME:{_Handler.origin_stamp}\n"
                    "#EXTINF:1.5,\n/seg.aac\n#EXT-X-ENDLIST\n"
                ).encode()
            else:
                body = MEDIA
        elif parsed.path == "/seg.aac":
            body = ADTS
        elif parsed.path == "/v1/programs/SYNTHETIC-2/chapters":
            body = CHAPTERS
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


def _serve():
    _Handler.paths = []
    _Handler.fail_auth = False
    _Handler.cancel_on_segment = False
    _Handler.stop = None
    _Handler.origin_stamp = None
    _Handler.login_body = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _env(monkeypatch, server, data: Path):
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("RADIKO_LOOPBACK_ORIGIN", origin)
    monkeypatch.setenv("RADIKO_AUTH_ORIGIN", origin)
    monkeypatch.setenv("RADIKO_NULL_SINK", "1")
    store = SessionStore(data)
    store.save(MemberSession("synthetic-token"))


def test_production_urls_keep_their_path_when_rewritten(monkeypatch):
    assert trusted("https://radiko.jp/v3/station/stream/pc_html5/TBS.xml")
    assert trusted("https://example.radiko-cf.com/live/master.m3u8")
    monkeypatch.delenv("RADIKO_LOOPBACK_ORIGIN", raising=False)
    assert (
        transport_url("https://api.radiko.jp/v2/api/auth1") == "https://api.radiko.jp/v2/api/auth1"
    )
    monkeypatch.setenv("RADIKO_LOOPBACK_ORIGIN", "http://127.0.0.1:9")
    assert (
        transport_url("https://radiko.jp/v3/station/stream/pc_html5/TBS.xml")
        == "http://127.0.0.1:9/v3/station/stream/pc_html5/TBS.xml"
    )
    built = append_query(
        "https://example.radiko-cf.com/live/master.m3u8",
        [("station_id", "TBS"), ("l", "15")],
    )
    assert built.startswith("https://example.radiko-cf.com/live/master.m3u8?")
    assert "station_id=TBS" in built and "l=15" in built


def test_play_sample_outputs_the_first_adts_segment(tmp_path, monkeypatch, capsys):
    """先頭セグメントの出力。null sink なので壁時計の再生時間ではない。"""
    server = _serve()
    _env(monkeypatch, server, tmp_path)
    started = time.perf_counter()
    try:
        code = main(["--data-dir", str(tmp_path), "play-sample", "TBS"])
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "先頭1セグメントの出力完了:" in out
    assert "サンプル末尾で停止しました" in out
    assert "stream-token" not in out + err
    assert time.perf_counter() - started < 5


def test_probe_live_follows_auth_playlist_and_adts(tmp_path, monkeypatch, capsys):
    server = _serve()
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(["--data-dir", str(tmp_path), "probe-live", "TBS"])
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "synthetic-token" not in out + err
    assert "stream-token" not in out + err
    assert "会員セッション有効" in err
    assert "TBS / Live /" in out
    assert "認証エリア: JP13 / エリア外: false" in out
    assert "HLS取得成功: 1セグメント / 1.500秒 / ENDLIST: true" in out
    assert f"AACセグメントのPyAVデコード成功: {PACKETS}パケット" in out
    assert "音声出力は行っていません" in out
    assert _Handler.paths[:5] == [
        "/ap/member/webapi/v2/member/login/check",
        "/v1/programs/list",
        "/v2/api/auth1",
        "/v2/api/auth2",
        "/v3/station/list/JP13.xml",
    ]
    assert "/v3/station/stream/pc_html5/TBS.xml" in _Handler.paths
    assert "/live/master.m3u8" in _Handler.paths
    assert "/media.m3u8" in _Handler.paths
    assert "/seg.aac" in _Handler.paths


def test_auth_rejection_is_the_stream_error_not_a_hardcoded_network_failure(
    tmp_path, monkeypatch, capsys
):
    server = _serve()
    _Handler.fail_auth = True
    _env(monkeypatch, server, tmp_path)
    try:
        code = main(["--data-dir", str(tmp_path), "probe-live", "TBS"])
    finally:
        server.shutdown()
    _out, err = capsys.readouterr()
    assert code == 1
    assert "配信認証: 配信認証が拒否されました" in err
    assert "/seg.aac" not in _Handler.paths


def test_chapters_and_timefree_play_use_the_same_program_endpoint(tmp_path, monkeypatch, capsys):
    server = _serve()
    _env(monkeypatch, server, tmp_path)
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d120000")
    try:
        chapter_code = main(["chapters", "TBS", yesterday])
        chapter_out, chapter_err = capsys.readouterr()
        play_started = time.perf_counter()
        play_code = main(
            ["--data-dir", str(tmp_path), "play-timefree", "TBS", yesterday, "--seconds", "1"]
        )
        elapsed = time.perf_counter() - play_started
    finally:
        server.shutdown()
    play_out, play_err = capsys.readouterr()
    assert chapter_code == 0, chapter_err
    assert "合成番組 / TBS /" in chapter_out
    assert "放送枠・イベント原点一致" in chapter_out
    assert "1: 10.000–20.000秒 本題" in chapter_out
    assert play_code == 0, play_err
    assert "指定時間で停止しました" in play_out
    assert "TimeFree" in play_out
    assert "/v1/programs/SYNTHETIC-2/chapters" in _Handler.paths
    # null sink は壁時計で1秒待たない。実機の再生時間ではない。
    assert elapsed < 5


def test_segment_cancel_stops_before_claiming_playback(tmp_path, monkeypatch, capsys):
    server = _serve()
    stop = threading.Event()
    _Handler.stop = stop
    _Handler.cancel_on_segment = True
    _env(monkeypatch, server, tmp_path)
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d120000")

    def cancel():
        return stop.is_set()

    from radiko.auth import MemberClient
    from radiko.prepare import play_station

    session = MemberSession("synthetic-token")
    membership = MemberClient.create().check(session)
    try:
        report = play_station(
            "TBS",
            "timefree",
            datetime.strptime(yesterday, "%Y%m%d%H%M%S").replace(tzinfo=JST),
            datetime.now(UTC),
            session,
            membership,
            0.0,
            1.0,
            cancel,
        )
    finally:
        server.shutdown()
    assert report.reason == "cancelled"
    assert report.frames == 0
    assert "synthetic-token" not in repr(report)


def test_tui_login_and_chapter_play_trim_the_segment(tmp_path, monkeypatch):
    """チャプターの秒位置はプレイリストの番組時刻から PCM を切り詰める。"""
    from radiko.domain import Program, ProgramKey
    from radiko_client.tui import Screen, perform_pending

    server = _serve()
    _env(monkeypatch, server, tmp_path)
    start = (datetime.now(JST) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    program = Program(ProgramKey("TBS", start), "slot", start + timedelta(hours=1), "対話番組")
    _Handler.origin_stamp = (start - timedelta(minutes=30)).isoformat()
    screen = Screen([program], datetime.now(UTC), offline=False, data_dir=tmp_path)
    screen.handle("c")
    try:
        perform_pending(screen)
        from radiko_client.tui import wait_jobs

        wait_jobs(screen)
        assert screen.chapters[0][0] == 10.0
        assert screen.chapters[0][2] == "本題"
        screen.area = "chapters"
        screen.handle("enter")
        assert screen.play_request == (program, 10.0)
        screen.play_request = None
        screen.chapters = [(0.4, 1.0, "途中")]
        screen.chapter_index = 0
        screen.handle("enter")
        perform_pending(screen)
        wait_jobs(screen)
        trimmed = screen.played_frames
        screen.chapters = [(0.0, 1.0, "先頭")]
        screen.handle("enter")
        perform_pending(screen)
        wait_jobs(screen)
        whole = screen.played_frames
    finally:
        server.shutdown()
    assert whole > trimmed > 0
    assert "synthetic-token" not in repr(screen.chapters)
