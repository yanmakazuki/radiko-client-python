"""P3（Issue #9）の非再帰局切替・[0] 廃止・import 整理・デッドコード除去を固定する。"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from radiko import prepare, storage
from radiko.domain import JST
from radiko.fragmented import _take, box, words
from radiko.media import _walk
from radiko.storage import MemberSession


def test_station_switch_does_not_grow_the_call_stack(monkeypatch):
    from radiko.prepare import play_station

    start = datetime.now(timezone.utc)
    program = type(
        "Program",
        (),
        {
            "start": start,
            "end": start + timedelta(hours=1),
            "title": "合成番組",
        },
    )()
    media = type(
        "Media",
        (),
        {
            "media_sequence": 0,
            "segments": [],
            "end_list": True,
            "target_duration": 1.0,
        },
    )()
    depths: list[int] = []
    switches = {"left": 1200}
    ready_calls: list[object] = []

    def fake_prepare(station, mode, cursor, now, session, membership, position, cancelled):
        depths.append(len(inspect.stack()))
        return program, {}, "JP13", False, ["http://127.0.0.1/media.m3u8"]

    def fake_open_media(url, headers, cancelled):
        return url, media

    def commands():
        if switches["left"] <= 0:
            return None
        switches["left"] -= 1
        return "live TBS"

    monkeypatch.setattr(prepare, "prepare", fake_prepare)
    monkeypatch.setattr(prepare, "_open_media", fake_open_media)
    report = play_station(
        "TBS",
        "live",
        datetime.now(JST),
        datetime.now(timezone.utc),
        MemberSession("token"),
        None,
        0.0,
        None,
        lambda: False,
        commands=commands,
        on_ready=ready_calls.append,
    )
    assert len(depths) == 1201
    assert max(depths) - min(depths) <= 2
    assert len(ready_calls) == 1
    assert report.reason == "program-end"


def test_counters_are_returned_instead_of_mutated_lists():
    data = box(b"ftyp", b"") + box(b"trun", words(0, 3))
    assert _walk(data, 0, 0, 0) == (2, 3)
    assert _take(words(7), 0) == (7, 4)
    assert _take(words(7), 0)[1] == 4


def test_no_unexpected_function_level_imports():
    allowed = {"radiko.reauth", "radiko.coreaudio", "radiko_client.cli", "termios", "tty"}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0
            self.modules: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Import(self, node: ast.Import) -> None:
            if self.depth:
                self.modules.extend(alias.name for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if self.depth and node.module:
                self.modules.append(node.module)

    found: list[str] = []
    for path in sorted(Path("src").rglob("*.py")):
        visitor = Visitor()
        visitor.visit(ast.parse(path.read_text()))
        found.extend(visitor.modules)
    unexpected = sorted(set(found) - allowed)
    assert unexpected == []
    assert allowed <= set(found)


def test_every_module_imports_alone_without_a_cycle():
    modules = [
        "radiko.auth",
        "radiko.catalog",
        "radiko.coreaudio",
        "radiko.fragmented",
        "radiko.http_client",
        "radiko.prepare",
        "radiko.reauth",
        "radiko_client.tui",
        "radiko_client.cli",
    ]
    for module in modules:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (module, result.stderr)


def test_dead_helpers_and_single_element_imports_are_gone():
    assert not hasattr(prepare, "_unused_split")
    assert not hasattr(prepare, "timedelta_utc")
    assert not hasattr(storage, "unicat")
    with pytest.raises(storage.SearchError):
        storage._check_name("a\x01b")
    source = Path(prepare.__file__).read_text()
    assert "if False else" not in source
    assert "urlsplit" not in source
