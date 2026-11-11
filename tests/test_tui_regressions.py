"""QA の4件。キーは疑似端末か service_loop に通し、取得と再生はローカルサーバへ出る。"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from radiko.domain import JST, BroadcastRange, Filter, Scope, SearchCriteria, broadcast_day
from radiko.fixtures import encode_adts_sine
from radiko.storage import MemberSession, SearchStore, SessionStore

UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures"
STREAMS = (FIXTURES / "streams.xml").read_bytes()
ADTS = encode_adts_sine(packets=4)


def _day_text(when: datetime) -> str:
    return broadcast_day(when).strftime("%Y%m%d")


def _stations(area: str, station: str) -> bytes:
    return (
        f'<stations area_id="{area}">'
        f"<station><id>{station}</id><name>試験</name></station></stations>"
    ).encode()


def _schedule(day: str, station: str, title: str) -> bytes:
    return (
        f'<radiko><stations><station id="{station}"><name>試験</name><progs>'
        f'<prog id="p" ft="{day}120000" to="{day}130000"><title>{title}</title></prog>'
        f"</progs></station></stations></radiko>"
    ).encode()


class _Server(BaseHTTPRequestHandler):
    paths: list[str] = []
    segs = 0
    hold_day = ""
    release = threading.Event()

    def do_GET(self):
        parsed = urlsplit(self.path)
        _Server.paths.append(parsed.path)
        if parsed.path.startswith("/v3/station/list/"):
            area = parsed.path.split("/")[-1].removesuffix(".xml")
            station = "TBS" if area == "JP13" else f"N{area[-2:]}"
            body = _stations(area, station)
        elif parsed.path.startswith("/v3/program/station/date/"):
            parts = parsed.path.split("/")
            day, station = parts[-2], parts[-1].removesuffix(".xml")
            if day == _Server.hold_day:
                _Server.release.wait(3)
            title = f"DAY{day}"
            body = _schedule(day, station, title)
        elif parsed.path == "/ap/member/webapi/v2/member/login/check":
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
            body = b"JP13,Tokyo\n"
        elif parsed.path == "/v1/programs/list":
            from urllib.parse import parse_qs

            when = datetime.fromisoformat(
                parse_qs(parsed.query)["lastStartAt"][0].replace("Z", "+00:00")
            )
            start = when - timedelta(seconds=1) - timedelta(minutes=30)
            end = when - timedelta(seconds=1) + timedelta(hours=2)
            body = json.dumps(
                {
                    "programs": [
                        {
                            "stationId": "TBS",
                            "title": "配信",
                            "startAt": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "endAt": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "eventId": "SYNTHETIC-2",
                            "episodeIntroductionStartAt": start.astimezone(UTC).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),
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
        elif parsed.path == "/v3/station/stream/pc_html5/TBS.xml":
            body = STREAMS
        elif parsed.path.endswith("/master.m3u8"):
            body = (
                b"#EXTM3U\n"
                b'#EXT-X-STREAM-INF:BANDWIDTH=48000,CODECS="mp4a.40.2",AUDIO="main"\n'
                b"unused.m3u8\n"
                b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="main",DEFAULT=YES,URI="/media.m3u8"\n'
            )
        elif parsed.path == "/media.m3u8":
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:1"]
            for index in range(8):
                lines.append("#EXTINF:1.0,")
                lines.append(f"/seg/{index}.aac")
            lines.append("#EXT-X-ENDLIST")
            body = ("\n".join(lines) + "\n").encode()
        elif parsed.path.startswith("/seg/"):
            time.sleep(0.4)
            _Server.segs += 1
            body = ADTS
        elif parsed.path == "/v1/programs/SYNTHETIC-2/chapters":
            body = json.dumps(
                {
                    "stationId": "TBS",
                    "programId": "SYNTHETIC-2",
                    "chapters": [
                        {
                            "title": "本題",
                            "summaryLong": "要約文" * 40,
                            "fromSec": 1,
                            "toSec": 20,
                        }
                    ],
                }
            ).encode()
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
    _Server.paths = []
    _Server.segs = 0
    _Server.hold_day = ""
    _Server.release = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _env(server, data: Path) -> dict[str, str]:
    origin = f"http://127.0.0.1:{server.server_port}"
    SessionStore(data).save(MemberSession("synthetic-token"))
    env = os.environ.copy()
    env.update(
        {
            "RADIKO_CATALOG_ORIGIN": origin,
            "RADIKO_LOOPBACK_ORIGIN": origin,
            "RADIKO_AUTH_ORIGIN": origin,
            "RADIKO_NULL_SINK": "1",
            "PYTHONPATH": "src",
            "TERM": "xterm",
        }
    )
    return env


def _pty(argv: list[str], env: dict[str, str], script) -> tuple[int, bytes]:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "radiko_client", "--data-dir", env["DATA"], *argv],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    os.close(slave)
    collected = b""
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    collected += os.read(master, 8192)
                except OSError:
                    break
            script(collected, master, process)
        else:
            if process.poll() is None:
                process.kill()
    finally:
        if process.poll() is None:
            process.wait(timeout=3)
        os.close(master)
    return process.returncode if process.returncode is not None else 1, collected


def test_q_stops_playback_while_segments_are_still_arriving(tmp_path):
    server = _serve()
    day = _day_text(datetime.now(JST) - timedelta(days=1))
    env = _env(server, tmp_path)
    env["DATA"] = str(tmp_path)
    sent = {"fetch": False, "play": False, "quit": False}
    started = {"q": 0.0}

    def script(collected: bytes, master: int, process: subprocess.Popen) -> None:
        if not sent["fetch"] and b"radiko-client" in collected:
            os.write(master, b"r")
            sent["fetch"] = True
        if sent["fetch"] and not sent["play"] and b"DAY" in collected:
            os.write(master, b"\r")
            sent["play"] = True
        if sent["play"] and not sent["quit"] and _Server.segs >= 1:
            started["q"] = time.monotonic()
            os.write(master, b"q")
            sent["quit"] = True

    try:
        code, collected = _pty(["tui", "TBS", day], env, script)
    finally:
        server.shutdown()
    assert sent["quit"], collected.decode(errors="replace")
    assert code == 0
    assert time.monotonic() - started["q"] < 2.5
    assert 0 < _Server.segs < 8


def test_filter_keys_send_area_and_dates_and_nationwide_lists_every_area(tmp_path):
    server = _serve()
    env = _env(server, tmp_path)
    env["DATA"] = str(tmp_path)
    phase = {"step": 0}

    def script(collected: bytes, master: int, process: subprocess.Popen) -> None:
        if phase["step"] == 0 and b"radiko-client" in collected:
            os.write(master, b"f\x1520261014\t20261015\t\x1b[12~" + b"j" * 13 + b"\r\r")
            phase["step"] = 1
        if phase["step"] == 1 and any(
            path.startswith("/v3/program/station/date/20261015/") for path in _Server.paths
        ):
            os.write(master, b"f\t\t\x1b[12~\r\r")
            phase["step"] = 2
        if (
            phase["step"] == 2
            and sum(path.startswith("/v3/station/list/JP") for path in _Server.paths) >= 47
        ):
            os.write(master, b"q")
            phase["step"] = 3

    try:
        code, _collected = _pty(["tui"], env, script)
    finally:
        server.shutdown()
    assert code == 0
    assert "/v3/station/list/JP13.xml" in _Server.paths
    assert "/v3/program/station/date/20261014/TBS.xml" in _Server.paths
    assert "/v3/program/station/date/20261015/TBS.xml" in _Server.paths
    areas = {
        path.split("/")[-1].removesuffix(".xml")
        for path in _Server.paths
        if path.startswith("/v3/station/list/JP")
    }
    assert areas >= {f"JP{number}" for number in range(1, 48)}


def test_named_recall_uses_the_saved_period(tmp_path):
    day = datetime(2026, 10, 14, 12, tzinfo=JST).date()
    SearchStore(tmp_path).save_named(
        "生活",
        SearchCriteria.create(
            Scope.station("TBS"),
            BroadcastRange.new(day, day + timedelta(days=1)),
            Filter.create([], [], False, False, None),
        ),
    )
    server = _serve()
    env = _env(server, tmp_path)
    env["DATA"] = str(tmp_path)
    sent = {"go": False, "done": False}

    def script(collected: bytes, master: int, process: subprocess.Popen) -> None:
        if not sent["go"] and b"radiko-client" in collected:
            # b で一覧、Enter で条件を編集画面へ、もう一度 Enter で確定して通信する。
            os.write(master, b"b\r\r")
            sent["go"] = True
        if "/v3/program/station/date/20261015/TBS.xml" in _Server.paths and not sent["done"]:
            sent["done"] = True
            os.write(master, b"q")

    try:
        code, collected = _pty(["tui"], env, script)
    finally:
        server.shutdown()
    text = collected.decode(errors="replace")
    assert code == 0, text
    assert "20261014〜20261015" in text
    assert "/v3/program/station/date/20261014/TBS.xml" in _Server.paths
    assert "/v3/program/station/date/20261015/TBS.xml" in _Server.paths


def test_chapter_tab_toggles_summary_through_the_terminal(tmp_path):
    server = _serve()
    day = _day_text(datetime.now(JST) - timedelta(days=1))
    env = _env(server, tmp_path)
    env["DATA"] = str(tmp_path)
    phase = {"n": 0}

    def script(collected: bytes, master: int, process: subprocess.Popen) -> None:
        if phase["n"] == 0 and b"radiko-client" in collected:
            os.write(master, b"r")
            phase["n"] = 1
        if phase["n"] == 1 and "DAY".encode() in collected:
            os.write(master, b"c")
            phase["n"] = 2
        if phase["n"] == 2 and "本題".encode() in collected:
            os.write(master, b"\t")
            phase["n"] = 3
        if phase["n"] == 3 and "Tab 章一覧".encode() in collected:
            os.write(master, b"j\tqq")
            phase["n"] = 4

    try:
        code, collected = _pty(["tui", "TBS", day], env, script)
    finally:
        server.shutdown()
    text = collected.decode(errors="replace")
    assert phase["n"] == 4, text[-400:]
    assert code == 0
    assert "選択したチャプターの要約（Tabで拡大・スクロール）" in text
    assert "要約（先頭1200文字まで）・上下/ページ スクロール・Tab 章一覧" in text


def test_stale_catalog_result_is_ignored(tmp_path, monkeypatch):
    server = _serve()
    _Server.hold_day = "20261014"
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("RADIKO_CATALOG_ORIGIN", origin)
    import queue

    from radiko_client.tui import Screen, service_loop

    screen = Screen([], datetime(2026, 10, 15, 12, tzinfo=JST), offline=False, data_dir=tmp_path)
    screen.criteria = SearchCriteria.create(
        Scope.station("TBS"),
        BroadcastRange.single(datetime(2026, 10, 14).date()),
        Filter.create(),
    )
    keys: queue.Queue[str | None] = queue.Queue()

    def read_key(timeout: float) -> str | None:
        try:
            return keys.get(timeout=timeout)
        except queue.Empty:
            return None

    worker = threading.Thread(target=service_loop, args=(screen, read_key, None), daemon=True)
    worker.start()
    try:
        keys.put("r")
        time.sleep(0.2)
        screen.criteria = SearchCriteria.create(
            Scope.station("TBS"),
            BroadcastRange.single(datetime(2026, 10, 15).date()),
            Filter.create(),
        )
        keys.put("r")
        time.sleep(0.1)
        _Server.release.set()
        for _ in range(50):
            if any(program.title == "DAY20261015" for program in screen.programs):
                break
            time.sleep(0.05)
        assert [program.title for program in screen.programs] == ["DAY20261015"]
        keys.put("q")
        worker.join(3)
    finally:
        server.shutdown()
    assert not worker.is_alive()
