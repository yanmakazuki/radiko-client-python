"""TUI の再確認、ライブ、秒指定、名前付き条件。再生そのものは要求だけを見る。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from radiko.domain import BroadcastRange, Filter, Program, ProgramKey, Scope, SearchCriteria
from radiko.storage import MemberSession, SearchStore, SessionStore
from radiko_client.tui import Screen

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=JST)


def _program(title: str, start: datetime, end: datetime) -> Program:
    return Program(ProgramKey("TBS", start), "id", end, title)


def _finished() -> Program:
    return _program("朝", NOW - timedelta(hours=6), NOW - timedelta(hours=5))


def _on_air() -> Program:
    return _program("いま", NOW - timedelta(hours=1), NOW + timedelta(hours=1))


def test_session_recheck_reads_the_saved_contract(tmp_path, monkeypatch):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/ap/member/webapi/v2/member/login/check"):
                body = b'{"status":"200","areafree":1,"timefreeplus":0}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("RADIKO_AUTH_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    SessionStore(tmp_path).save(MemberSession("synthetic-token"))
    screen = Screen([], NOW, offline=False, data_dir=tmp_path)
    try:
        screen.handle("a")
        from radiko_client.tui import perform_pending

        perform_pending(screen)
    finally:
        server.shutdown()
    assert screen.auth == "signed-in"
    assert screen.notice is not None
    assert "ログイン有効" in screen.notice.text
    assert "エリアフリー: あり" in screen.notice.text
    assert "タイムフリー30: なし" in screen.notice.text
    assert "synthetic-token" not in screen.notice.text


def test_live_and_seconds_follow_the_slot_rules():
    screen = Screen([_finished(), _on_air()], NOW, offline=False)
    screen.handle("l")
    assert screen.play_request is None
    assert screen.notice is not None
    assert "放送終了のためライブ再生できません" in screen.notice.text
    screen.selected = 1
    screen.handle("l")
    assert screen.play_request is not None
    assert screen.play_mode == "live"
    assert screen.play_request[0].title == "いま"
    screen.play_request = None
    screen.playing = _finished()
    screen.play_mode = "timefree"
    screen.handle("g")
    assert screen.area == "seconds"
    for digit in "120":
        screen.handle(digit)
    screen.handle("enter")
    assert screen.play_request == (screen.playing, 120.0)
    screen.play_request = None
    screen.playing = _on_air()
    screen.play_mode = "live"
    screen.handle("g")
    assert screen.area == "list"
    assert "ライブは位置指定できません" in screen.notice.text


def test_named_search_is_loaded_from_the_saved_file(tmp_path):
    criteria = SearchCriteria.create(
        Scope.station("TBS"),
        BroadcastRange.new(NOW.date(), NOW.date()),
        Filter.create([], [], False, False, "生活"),
    )
    SearchStore(tmp_path).save_named("生活", criteria)
    screen = Screen([], NOW, offline=False, data_dir=tmp_path)
    screen.handle("b")
    assert screen.area == "named"
    assert "生活" in "\n".join(screen.render())
    screen.handle("enter")
    assert screen.area == "filter"
    assert screen.filter_form is not None
    assert screen.filter_form.keyword.value == "生活"
    assert screen.filter_form.scope.value == "局:TBS"
    assert screen.fetch_programs is False
    assert "「生活」を呼び出しました" in (screen.notice.text or "")
    screen.handle("enter")
    assert screen.area == "list"
    assert screen.committed.keyword == "生活"
    assert screen.fetch_programs is True
