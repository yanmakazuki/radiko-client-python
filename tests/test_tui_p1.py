"""P1（Issue #17）: 起動時復元と終了時保存、観測分類ピッカー、名前付き条件、再生コマンド。"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from radiko.domain import (
    JST,
    BroadcastRange,
    Classification,
    Filter,
    Program,
    ProgramKey,
    Scope,
    SearchCriteria,
    broadcast_day,
)
from radiko.storage import SearchStore
from radiko_client import tui
from radiko_client.tui import Screen, startup_state

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=JST)
UTC = timezone.utc


def _program(title: str, start: datetime, end: datetime, **genres) -> Program:
    return Program(ProgramKey("TBS", start), "id", end, title, **genres)


def _criteria(keyword: str | None = None) -> SearchCriteria:
    return SearchCriteria.create(
        Scope.station("TBS"),
        BroadcastRange.new(datetime(2026, 10, 14).date(), datetime(2026, 10, 15).date()),
        Filter.create(["C103"], [], False, False, keyword),
    )


def test_startup_state_restores_last_and_prefers_explicit_arguments(tmp_path):
    SearchStore(tmp_path).save_last(_criteria("生活"))
    criteria, store, notice = startup_state([], tmp_path, NOW, False)
    assert store is not None
    assert notice == ""
    assert criteria.scope == Scope.station("TBS")
    assert criteria.range.start == broadcast_day(NOW)
    assert criteria.filter.keyword == "生活"

    explicit, same_store, _notice = startup_state(["TBS", "20261010"], tmp_path, NOW, False)
    assert same_store is not None
    assert explicit.range.start.strftime("%Y%m%d") == "20261010"
    assert explicit.filter.keyword is None

    _criteria_offline, offline_store, offline_notice = startup_state([], tmp_path, NOW, True)
    assert offline_store is None
    assert offline_notice == ""


def test_run_starts_with_a_fetch_and_a_session_check_then_saves_last(tmp_path, monkeypatch):
    SearchStore(tmp_path).save_last(_criteria("生活"))
    captured: list[Screen] = []

    monkeypatch.setattr(tui, "_terminal_loop", lambda screen: captured.append(screen))
    monkeypatch.setattr(tui.sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(tui.sys, "stdout", type("TTY", (), {"isatty": lambda self: True})())
    tui.run([], tmp_path)

    assert len(captured) == 1
    screen = captured[0]
    assert screen.auth_refresh is True
    assert screen.fetch_programs is True
    assert screen.generation == 1
    assert screen.criteria is not None
    assert screen.criteria.filter.keyword == "生活"
    saved = SearchStore(tmp_path).load().last
    assert saved is not None
    assert saved.range.start == screen.criteria.range.start
    assert saved.filter.keyword == "生活"


def test_filter_picker_collects_observed_classifications_not_placeholders():
    programs = [
        _program(
            "音楽番組",
            NOW - timedelta(hours=1),
            NOW,
            program_genres=[Classification("C103", "音楽")],
            personality_genres=[Classification("C999", "出演者")],
        )
    ]
    screen = Screen(programs, NOW, offline=True)
    screen.handle("f")
    assert screen.area == "filter"
    for _ in range(3):
        screen.handle("tab")
    screen.handle("f2")
    form = screen.filter_form
    assert form is not None
    assert form.picker is not None
    assert form.picker_ids == ["C103"]
    body = "\n".join(form.picker_rows())
    assert "番組 C103: 音楽" in body
    assert "UNKNOWN" not in body
    assert "C999 出演分類" not in body
    screen.handle("j")
    screen.handle("j")
    screen.handle(" ")
    screen.handle("enter")
    assert form.genres.value == "C103"
    assert form.picker is None
    screen.handle("tab")
    screen.handle("f2")
    assert form.picker_ids == ["C999"]
    assert "出演者 C999: 出演者" in "\n".join(form.picker_rows())


def test_o_opens_the_filter_with_the_selected_station_scope():
    programs = [
        _program("朝刊", NOW - timedelta(hours=1), NOW),
        _program("夜刊", NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
    ]
    screen = Screen(programs, NOW, offline=True)
    screen.selected = 0
    screen.handle("o")
    assert screen.area == "filter"
    assert screen.filter_form is not None
    assert screen.filter_form.scope.value == "局:TBS"


def test_named_screen_lists_saves_and_deletes(tmp_path):
    SearchStore(tmp_path).save_named("生活", _criteria())
    screen = Screen([], NOW, offline=False, data_dir=tmp_path)
    screen.criteria = _criteria()
    screen.committed = screen.criteria.filter
    screen.handle("b")
    assert screen.area == "named"
    body = "\n".join(screen.render())
    assert "> 生活" in body
    assert "TBS / 2026-10-14〜2026-10-15" in body

    screen.handle("n")
    assert screen.named_mode == "name"
    assert "Enter 保存" in "\n".join(screen.render())
    for char in "朝刊":
        screen.handle(char)
    screen.handle("enter")
    assert screen.named_mode == "browse"
    assert "朝刊" in SearchStore(tmp_path).load().named

    names = list(screen.named)
    assert set(names) == {"生活", "朝刊"}
    screen.named_index = 0
    screen.handle("j")
    assert screen.named_index == 1
    screen.handle("k")
    assert screen.named_index == 0
    target = names[0]
    screen.handle("d")
    assert screen.named_mode == "delete"
    assert "削除しますか" in "\n".join(screen.render())
    screen.handle("y")
    assert screen.named_mode == "browse"
    assert target not in SearchStore(tmp_path).load().named
    assert len(SearchStore(tmp_path).load().named) == 1
    assert screen.area == "named"


def test_playback_keys_reach_the_background_play_job():
    program = _program("いま", NOW - timedelta(minutes=10), NOW + timedelta(minutes=50))
    screen = Screen([program], NOW, offline=False)
    screen.playing = program
    screen.play_mode = "timefree"
    screen.play_commands = queue.Queue()
    screen.job_cancel = threading.Event()

    screen.handle(" ")
    assert screen.paused is True
    assert screen.play_commands.get_nowait() == "pause"
    screen.handle(" ")
    assert screen.paused is False
    assert screen.play_commands.get_nowait() == "resume"
    screen.handle("+")
    assert screen.volume == 30
    assert screen.play_commands.get_nowait() == "volume 30"
    screen.handle("right")
    assert screen.position == 15
    assert screen.play_commands.get_nowait() == "+15"
    screen.handle("left")
    assert screen.position == 0
    assert screen.play_commands.get_nowait() == "-15"

    screen.handle("s")
    assert screen.job_cancel.is_set()
    assert screen.playing is None
    assert screen.paused is False
    assert screen.play_commands.empty()


def test_header_shows_the_committed_criteria():
    screen = Screen([], NOW, offline=False)
    screen.criteria = _criteria("生活")
    screen.committed = screen.criteria.filter
    lines = screen.render()
    assert lines[1].endswith("（日本時間・05:00区切り）")
    assert "TBS / 2026-10-14〜2026-10-15" in lines[1]
    assert "キーワード: 生活" in lines[2]
    assert "番組ジャンル: C103 / 出演者分類: 指定なし" in lines[3]
    screen.committed = Filter.create(["C103"], [], True, False, None)
    screen.criteria = SearchCriteria.create(
        Scope.nationwide(),
        BroadcastRange.single(datetime(2026, 10, 15).date()),
        screen.committed,
    )
    lines = screen.render()
    assert "全国 / 2026-10-15〜2026-10-15" in lines[1]
    assert "キーワード: 指定なし" in lines[2]
    assert "C103,未分類 / 出演者分類: 指定なし" in lines[3]


def test_commands_and_stop_reach_the_running_play_job(tmp_path, monkeypatch):
    """Space でセグメント取得が止まり、再開で続き、s で再生ジョブが cancel される。"""
    from radiko.stream import parse_time
    from test_tui_regressions import _env, _serve, _Server

    server = _serve()
    env = _env(server, tmp_path)
    for key, value in env.items():
        if key.startswith("RADIKO_"):
            monkeypatch.setenv(key, value)
    start = parse_time((datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d120000"))
    program = _program("配信", start, start + timedelta(hours=2))
    screen = Screen([program], datetime.now(JST), offline=False, data_dir=tmp_path)
    screen.criteria = SearchCriteria.create(
        Scope.station("TBS"), BroadcastRange.single(start.date()), Filter.create()
    )
    keys: queue.Queue = queue.Queue()

    def read_key(timeout: float) -> str | None:
        try:
            return keys.get(timeout=timeout)
        except queue.Empty:
            return None

    worker = threading.Thread(target=tui.service_loop, args=(screen, read_key, None), daemon=True)
    worker.start()
    try:
        keys.put("enter")
        deadline = time.monotonic() + 20
        while _Server.segs < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _Server.segs >= 1, "再生がセグメントに届いていない"
        keys.put(" ")
        time.sleep(0.8)
        paused_at = _Server.segs
        assert screen.paused is True
        assert screen.play_commands is None or screen.play_commands.empty()
        time.sleep(0.8)
        assert _Server.segs == paused_at, "一時停止中も取得が進んだ"
        keys.put(" ")
        deadline = time.monotonic() + 10
        while _Server.segs <= paused_at and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _Server.segs > paused_at, "再開しても取得が進まない"
        keys.put("s")
        time.sleep(1.0)
        stopped_at = _Server.segs
        assert screen.playing is None
        time.sleep(1.0)
        assert _Server.segs == stopped_at, "停止後も取得が進んだ"
        keys.put("q")
        worker.join(5)
    finally:
        if worker.is_alive():
            keys.put("q")
            worker.join(5)
        server.shutdown()
    assert not worker.is_alive()
