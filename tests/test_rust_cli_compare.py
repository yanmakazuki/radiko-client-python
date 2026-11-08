"""同じ引数で Rust バイナリと Python の終了コード・標準出力・標準エラーを比べる。

バイナリが無い CI では飛ばす。実 radiko には繋がない。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "chapters.json"
CANDIDATES = [
    os.environ.get("RADIKO_RUST_BIN", ""),
    "/tmp/rust-src/radiko-client/target/debug/radiko-client",
]
RUST = next((path for path in CANDIDATES if path and Path(path).is_file()), "")

pytestmark = pytest.mark.skipif(not RUST, reason="pinned Rust CLI binary is not built here")


def _run(argv: list[str], binary: str, data: Path | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.pop("RADIKO_LOOPBACK_ORIGIN", None)
    env.pop("RADIKO_CATALOG_ORIGIN", None)
    env.pop("RADIKO_AUTH_ORIGIN", None)
    env.pop("RADIKO_NULL_SINK", None)
    command = [binary, *argv]
    if data is not None and "--data-dir" not in argv:
        command = [binary, "--data-dir", str(data), *argv]
    completed = subprocess.run(
        command,
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _run_python(argv: list[str], data: Path | None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.pop("RADIKO_LOOPBACK_ORIGIN", None)
    env.pop("RADIKO_CATALOG_ORIGIN", None)
    env.pop("RADIKO_AUTH_ORIGIN", None)
    env.pop("RADIKO_NULL_SINK", None)
    command = [sys.executable, "-m", "radiko_client", *argv]
    if data is not None and "--data-dir" not in argv:
        command = [sys.executable, "-m", "radiko_client", "--data-dir", str(data), *argv]
    completed = subprocess.run(
        command,
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _assert_same(argv: list[str], data: Path | None = None) -> None:
    rust_argv = [str(data / "rust") if part == "DATA" else part for part in argv]
    python_argv = [str(data / "py") if part == "DATA" else part for part in argv]
    rust = _run(rust_argv, RUST, None)
    python = _run_python(python_argv, None)
    assert python[0] == rust[0], (argv, rust, python)
    assert python[1] == rust[1], (argv, rust[1], python[1])
    assert python[2] == rust[2], (argv, rust[2], python[2])


def test_offline_help_and_errors_match_the_rust_process(tmp_path: Path):
    data = tmp_path / "data"
    for argv in [
        ["--help"],
        ["programs", "--help"],
        ["searches", "--help"],
        ["tui", "--help"],
        ["output-info", "--help"],
        ["probe-output", "--help"],
        ["play-live", "--help"],
        ["chapters", "--help"],
        ["nope"],
        ["session", "--data-dir"],
        ["programs"],
        ["programs", "../TBS"],
        ["programs", "TBS", "20260230"],
        ["output-info", "7999"],
        ["probe-output", "11"],
        ["play-live", "../TBS"],
        ["play-timefree", "TBS", "20990101000000"],
        ["chapters", "TBS", "20260230000000"],
        ["login"],
        ["inspect-chapters", str(FIXTURE), "90", "0"],
    ]:
        _assert_same(argv)
    _assert_same(["--data-dir", "DATA", "session"], data)
    _assert_same(["--data-dir", "DATA", "logout"], data)
    _assert_same(
        [
            "--data-dir",
            "DATA",
            "searches",
            "save",
            "生活",
            "TBS",
            "20261014",
            "20261015",
            "--keyword",
            "生活",
        ],
        data,
    )
    _assert_same(["--data-dir", "DATA", "searches", "show", "生活"], data)
    _assert_same(["--data-dir", "DATA", "searches", "list"], data)
    _assert_same(["--data-dir", "DATA", "searches", "restore"], data)
    _assert_same(["--data-dir", "DATA", "searches", "delete", "生活"], data)
