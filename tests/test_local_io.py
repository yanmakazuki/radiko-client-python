"""ローカルサーバと ALSA の欠測。実 radiko と実アカウントは使わない。"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from radiko.auth import authenticate, partial_key
from radiko.storage import MemberSession
from radiko_client.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


class _Catalog(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/v3/program/station/date/20261015/TBS.xml"):
            body = (FIXTURES / "schedule.xml").read_bytes()
        elif self.path.startswith("/v3/station/list/JP13.xml"):
            body = (FIXTURES / "stations.xml").read_bytes()
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


def test_programs_json_uses_fixture_counts_not_the_file_as_an_oracle(tmp_path, capsys, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("RADIKO_CATALOG_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        code = main(["--data-dir", str(tmp_path / "data"), "programs", "TBS", "20261015", "--json"])
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert code == 1
    assert "不正な放送枠" in err
    assert payload["station_id"] == "TBS"
    assert payload["source_program_count"] == 3
    assert payload["invalid_count"] == 1
    assert payload["duplicate_count"] == 1
    assert payload["complete"] is False
    assert payload["programs"][1]["title"] == "朝 & 続編"
    assert payload["programs"][1]["status"] == "finished"


def test_programs_fixture_server_prints_titles(tmp_path, capsys, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("RADIKO_CATALOG_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        code = main(["--data-dir", str(tmp_path / "data"), "programs", "TBS", "20261015"])
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert "朝 & 続編" in out
    assert "テスト局 (TBS)" in out
    assert "不正" in out or code == 1
    assert code == 1
    assert "不正な放送枠" in err


def test_programs_area_acquisition_prints_lists_failures_and_station_names(
    tmp_path, capsys, monkeypatch
):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("RADIKO_CATALOG_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        code = main(
            ["--data-dir", str(tmp_path / "data"), "programs", "--areas", "JP13", "20261015"]
        )
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 1
    assert "テスト局 (TBS)" in out
    assert "朝 & 続編" in out
    assert "部分取得・未取得あり" in out
    assert "未取得番組表 RN1 2026-10-15" in err
    assert "取得結果に欠落があります" in err


def test_programs_multi_day_station_uses_the_fetched_station_name(tmp_path, capsys, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("RADIKO_CATALOG_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        code = main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "programs",
                "TBS",
                "20261015",
                "20261016",
                "--json",
            ]
        )
    finally:
        server.shutdown()
    out, err = capsys.readouterr()
    assert code == 1
    assert json.loads(out)["stations"] == [{"areas": [], "id": "TBS", "name": "テスト局"}]
    assert "取得結果に欠落があります" in err


def test_partial_key_and_auth_round_trip_hide_the_token(monkeypatch):
    assert partial_key(0, 4) == "YmNkMQ=="
    seen = {}

    class Reply:
        def __init__(self, headers, body):
            self.headers = headers
            self.body = body

    def fetch(path, headers):
        seen.setdefault("paths", []).append(path)
        blob = json.dumps(headers)
        assert "synthetic-password" not in blob
        if path.endswith("auth1"):
            assert headers["cookie"] == "radiko_session=synthetic-token"
            return Reply(
                {
                    "x-radiko-authtoken": "stream-token",
                    "x-radiko-keyoffset": "0",
                    "x-radiko-keylength": "4",
                },
                b"",
            )
        assert headers["x-radiko-partialkey"] == "YmNkMQ=="
        return Reply({}, b"JP13,Tokyo\n")

    headers, area = authenticate(fetch, MemberSession("synthetic-token"))
    assert area == "JP13"
    assert headers["x-radiko-authtoken"] == "stream-token"
    assert seen["paths"] == ["/v2/api/auth1", "/v2/api/auth2"]
    assert "synthetic-token" not in str(headers)


def test_missing_alsa_device_message_and_optional_library_log(tmp_path):
    config = tmp_path / "asound.conf"
    config.write_text("pcm.!default {\n type hw\n card MissingRadikoCard\n}\n")
    env = os.environ.copy()
    env.pop("RADIKO_ALSA_LOG", None)
    env.pop("RADIKO_NULL_SINK", None)
    env["ALSA_CONFIG_PATH"] = str(config)
    quiet = subprocess.run(
        [sys.executable, "-m", "radiko_client", "output-info"],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert quiet.returncode == 1
    assert quiet.stderr == "エラー: 既定の音声出力機器がありません\n"
    assert quiet.stdout == ""
    env["RADIKO_ALSA_LOG"] = "1"
    verbose = subprocess.run(
        [sys.executable, "-m", "radiko_client", "output-info"],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "ALSA lib" in verbose.stderr
    assert "既定の音声出力機器がありません" in verbose.stderr


def test_null_sink_probe_counts_frames(monkeypatch, capsys):
    """単体の null sink。1秒分の無音を壁時計では待たない。実機の再生時間ではない。"""
    monkeypatch.setenv("RADIKO_NULL_SINK", "1")
    assert main(["probe-output", "--mute", "1"]) == 0
    out, err = capsys.readouterr()
    assert "無音出力完了: 48000フレーム / 48000Hz / f32 / 2ch" in out
    assert err == ""
