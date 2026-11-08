"""閉じた標準出力は終了コード 141。標準エラーにはエラー行を足さない。"""

from __future__ import annotations

import os
import subprocess
import sys


def test_closed_stdout_exits_141_without_an_error_line():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    process = subprocess.Popen(
        [sys.executable, "-m", "radiko_client", "--help"],
        stdout=write_fd,
        stderr=subprocess.PIPE,
        env=env,
    )
    os.close(write_fd)
    _out, err = process.communicate(timeout=10)
    assert process.returncode == 141
    assert b"\xe3\x82\xa8\xe3\x83\xa9\xe3\x83\xbc:" not in err
    assert b"Broken pipe" not in err
