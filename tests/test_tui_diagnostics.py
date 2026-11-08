"""TUI の機器診断と取得状況。診断スレッドの古い結果は捨てる。"""

from __future__ import annotations

import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from radiko.domain import JST
from radiko.output import OutputCapability, OutputError, diagnose_default_output
from radiko_client.tui import DEVICE_GUIDANCE, DEVICE_NONE, Screen, perform_pending


def test_device_panel_keeps_the_newer_probe_and_names_a_missing_device():
    screen = Screen([], datetime(2026, 10, 15, 6, 30, tzinfo=JST), offline=False)
    gate = threading.Event()
    calls: list[int] = []

    lock = threading.Lock()

    def probe(rate: int) -> OutputCapability:
        with lock:
            turn = len(calls)
            calls.append(turn)
        if turn == 0:
            gate.wait(2)
        return OutputCapability("alsa", f"dev{turn}", rate, True, False)

    screen.device_probe = probe
    screen.area = "device"
    screen._request_device()
    screen._request_device()
    for _ in range(50):
        screen.poll_device()
        if screen.device_status == "ready":
            break
        threading.Event().wait(0.02)
    text = "\n".join(screen.render())
    assert "ホスト: alsa" in text
    assert "機器: dev1" in text
    assert "f32 mono 48000Hz: 対応" in text
    assert "f32 stereo 48000Hz: 非対応" in text
    gate.set()
    threading.Event().wait(0.05)
    screen.poll_device()
    assert "dev1" in "\n".join(screen.render())

    def missing(_rate: int) -> OutputCapability:
        raise OutputError("none")

    screen.device_probe = missing
    screen._request_device()
    for _ in range(50):
        screen.poll_device()
        if screen.device_status == "failed":
            break
        threading.Event().wait(0.02)
    failed = "\n".join(screen.render())
    assert DEVICE_NONE in failed
    assert DEVICE_GUIDANCE in failed


def test_this_host_reports_a_device_or_the_no_device_error():
    """この Linux で音が出るかは断言しない。無いときはその文面になる。"""
    try:
        report = diagnose_default_output(48000)
    except OutputError as error:
        assert error.code == "none"
        return
    assert report.host in {"alsa", "null", "CoreAudio"}
    assert report.sample_rate == 48000


def test_program_fetch_failure_is_listed_on_the_status_panel(tmp_path, monkeypatch):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(500)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("RADIKO_CATALOG_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    from radiko.domain import BroadcastRange, Filter, Scope, SearchCriteria

    screen = Screen([], datetime(2026, 10, 15, 12, 0, tzinfo=JST), offline=False, station_id="TBS")
    screen.criteria = SearchCriteria.create(
        Scope.station("TBS"),
        BroadcastRange.single(datetime(2026, 10, 15, tzinfo=JST).date()),
        Filter.create(),
    )
    try:
        screen.handle("r")
        perform_pending(screen)
        from radiko_client.tui import wait_jobs

        wait_jobs(screen)
    finally:
        server.shutdown()
    screen.area = "failures"
    text = "\n".join(screen.render())
    assert "部分取得・未取得あり" in text
    assert "局・日失敗1" in text
    assert "番組表 TBS 20261015 / 番組表サーバーがHTTP 500を返しました" in text
    assert "未取得・不正枠" not in text or "取得状況・未取得範囲" in text
