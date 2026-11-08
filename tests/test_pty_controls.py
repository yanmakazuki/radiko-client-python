"""疑似端末で --interactive と TUI の生入力を動かす。壁時計の再生時間ではない。"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import termios
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from radiko.domain import JST
from radiko.media import encode_adts_sine
from radiko.storage import MemberSession, SessionStore

UTC = timezone.utc
ADTS = encode_adts_sine(packets=20)
STREAMS = (Path(__file__).parent / "fixtures" / "streams.xml").read_bytes()
STATIONS = (Path(__file__).parent / "fixtures" / "stations.xml").read_bytes()


def _program(when: datetime) -> bytes:
    import json

    start = when - timedelta(minutes=30)
    end = when + timedelta(hours=2)
    return json.dumps(
        {
            "programs": [
                {
                    "stationId": "TBS",
                    "title": "対話番組",
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


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
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
            body = b"JP13,Tokyo\n"
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
            body = (
                b"#EXTM3U\n"
                b'#EXT-X-STREAM-INF:BANDWIDTH=48000,CODECS="mp4a.40.2",AUDIO="main"\n'
                b"unused.m3u8\n"
                b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="main",DEFAULT=YES,URI="/media.m3u8"\n'
            )
        elif parsed.path == "/media.m3u8":
            body = (
                b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n"
                b"#EXTINF:1.5,\n/seg.aac\n#EXT-X-ENDLIST\n"
            )
        elif parsed.path == "/seg.aac":
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


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run(argv: list[str], env: dict[str, str], script: bytes, until: bytes) -> tuple[int, bytes]:
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [sys.executable, "-m", "radiko_client", *argv],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    collected = b""
    sent = False
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    collected += os.read(master, 4096)
                except OSError:
                    break
            if not sent and until in collected:
                os.write(master, script)
                sent = True
            if process.poll() is not None and not ready:
                break
        else:
            process.kill()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        after = termios.tcgetattr(slave)
        os.close(slave)
        os.close(master)
    assert sent, collected.decode(errors="replace")
    assert after == before
    return process.returncode, collected


def test_interactive_pty_seeks_rejects_unknown_keys_and_cancels(tmp_path):
    server = _serve()
    origin = f"http://127.0.0.1:{server.server_port}"
    SessionStore(tmp_path).save(MemberSession("synthetic-token"))
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d120000")
    env = os.environ.copy()
    env.update(
        {
            "RADIKO_LOOPBACK_ORIGIN": origin,
            "RADIKO_AUTH_ORIGIN": origin,
            "RADIKO_NULL_SINK": "1",
            "PYTHONPATH": "src",
            "TERM": "xterm",
        }
    )
    try:
        code, collected = _run(
            ["--data-dir", str(tmp_path), "play-timefree", "TBS", yesterday, "--interactive"],
            env,
            b"status\nseek 1\nstatus\nnope\n+15\nq\n",
            "操作:".encode(),
        )
    finally:
        server.shutdown()
    text = collected.decode()
    assert code == 0, text
    assert "再生位置: 0.000秒" in text
    assert "再生位置: 1.000秒" in text
    assert "を入力してください" in text
    assert "中断しました" in text
    assert "synthetic-token" not in text
    assert "stream-token" not in text


def test_tui_pty_restores_the_terminal_after_quit(tmp_path):
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    env = os.environ.copy()
    env.update({"PYTHONPATH": "src", "TERM": "xterm"})
    process = subprocess.Popen(
        [sys.executable, "-m", "radiko_client", "tui", "--offline"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    collected = b""
    sent = False
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    collected += os.read(master, 4096)
                except OSError:
                    break
            if not sent and collected:
                os.write(master, b"?qq")
                sent = True
            if process.poll() is not None and not ready:
                break
        else:
            process.kill()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        after = termios.tcgetattr(slave)
        os.close(slave)
        os.close(master)
    text = collected.decode(errors="replace")
    assert sent, text
    assert process.returncode == 0, text
    assert "オフライン" in text
    assert after == before
