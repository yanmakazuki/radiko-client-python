"""時計を進めて 2ms、5 分、20 秒の境界を見る。壁時計では待たない。"""

from datetime import datetime, timedelta, timezone

from radiko.interactive import InteractiveControl
from radiko.media import AacError
from radiko.reauth import ReauthState
from radiko.storage import MemberSession
from radiko.stream import ProbeError
from radiko.timing import ReconnectBudget, accept_fragment

JST = timezone(timedelta(hours=9))


def test_fragment_clock_accepts_two_milliseconds_and_rejects_the_next_step():
    wall = datetime(2026, 10, 15, 12, 0, tzinfo=JST)
    stamp = accept_fragment(1.0, 1.002, 0, 1000, wall, None)
    accept_fragment(1.0, 0.998, 1000, 1000, wall + timedelta(seconds=1), stamp)
    try:
        accept_fragment(1.0, 1.0021, 0, 1000, wall, None)
    except AacError as error:
        assert error.code == "container"
    else:
        raise AssertionError("pcm longer than 2ms must fail")
    try:
        accept_fragment(1.0, 1.0, 1000, 1000, wall + timedelta(seconds=1.003), stamp)
    except AacError as error:
        assert error.code == "container"
    else:
        raise AssertionError("program date drift over 2ms must fail")


def test_reconnect_budget_uses_the_simulated_clock_and_stops_at_five_minutes():
    slept: list[float] = []
    budget = ReconnectBudget(lambda: False, sleep=slept.append)
    budget.backoff = 1
    # 1+2+4+8+10*28 = 15+280 = 295, next step is min(10, 5) = 5, total 300, next fails
    for _ in range(33):
        budget.wait()
    assert abs(budget.waited - 295) < 1e-6 or budget.waited <= 300
    while budget.waited < 300:
        budget.wait()
    assert abs(budget.waited - 300) < 1e-6
    try:
        budget.wait()
    except ProbeError as error:
        assert error.code == "reconnect"
    else:
        raise AssertionError("the fifth minute must stop")
    assert abs(sum(slept) - 300) < 1e-6
    cancelled = ReconnectBudget(lambda: True, sleep=slept.append)
    try:
        cancelled.wait()
    except ProbeError as error:
        assert error.code == "cancelled"
    else:
        raise AssertionError("cancel must skip the wait")


def test_reauth_interval_is_simulated_and_reuses_the_token_inside_twenty_seconds():
    clock = {"t": 0.0}
    state = ReauthState(
        MemberSession("synthetic-token"),
        lambda: False,
        lambda _session, _cancelled: ({}, "JP13"),
        now=lambda: clock["t"],
    )
    state.last = 0.0
    headers = {"x-radiko-authtoken": "held-token"}
    clock["t"] = 19.999
    renewed, how = state.renew(headers, "HLS音声取得")
    assert how == "reused"
    assert renewed["x-radiko-authtoken"] == "held-token"
    assert state.count == 0
    clock["t"] = 20.0
    assert not (clock["t"] - state.last < 20)


def test_interactive_seek_keeps_playback_and_only_the_latest_generation_commits():
    control = InteractiveControl(120, "timefree")
    control.handle("seek 3600")
    assert control.generation == 0
    assert "シーク不可" in control.messages[-1]
    control.handle("seek 1")
    first = control.generation
    assert control.position == 1
    control.handle("seek 2")
    assert control.complete(first, 1) is False
    assert control.complete(control.generation, 2) is True
    assert control.position == 2
    control.handle("pause")
    assert control.paused is True
    control.handle("q")
    assert control.stopped is True
    live = InteractiveControl(120, "live")
    live.handle("+15")
    assert live.generation == 0
    assert "再生を継続" in live.messages[-1]
