"""TUI の番組表取得が catalog の4並列取得を使う。"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta

from radiko.catalog import CatalogError, ProgramList
from radiko.domain import BroadcastRange, Filter, Scope, SearchCriteria
from radiko_client.tui import _fetch_programs


def test_tui_fetch_uses_parallel_catalog_acquisition(monkeypatch):
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    class FakeClient:
        def __init__(self, _fetch):
            pass

        def programs(self, station, day):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.1)
            with lock:
                state["active"] -= 1
            if day == date(2026, 10, 18):
                raise CatalogError("network")
            return ProgramList(station, station, day, [])

        def stations(self, area):
            return []

    monkeypatch.setattr("radiko.catalog.CatalogClient", FakeClient)
    start = date(2026, 10, 15)
    criteria = SearchCriteria.create(
        Scope.station("TBS"),
        BroadcastRange.new(start, start + timedelta(days=3)),
        Filter.create(),
    )
    started = time.perf_counter()
    result = _fetch_programs(criteria, threading.Event())
    elapsed = time.perf_counter() - started
    assert state["peak"] >= 2
    assert elapsed < 0.38
    assert result["schedule_failures"] == [("TBS", "20261018", "番組表サーバーと通信できません")]
    assert result["programs"] == []
    assert result["invalid"] == 0
