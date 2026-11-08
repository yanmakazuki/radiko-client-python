"""SIGINT は停止トークンにだけ入り、traceback を出さない。"""

from __future__ import annotations

import os
import signal
import threading

from radiko_client.cli import _restore_sigint, _sigint_stop


def test_sigint_sets_the_stop_event_without_keyboardinterrupt():
    stop = threading.Event()
    previous = _sigint_stop(stop)
    try:
        os.kill(os.getpid(), signal.SIGINT)
        assert stop.wait(1)
    finally:
        _restore_sigint(previous)


def test_muted_probe_exits_zero_on_sigint_without_traceback(monkeypatch, capsys):
    from types import SimpleNamespace

    from radiko_client import cli

    def fake_probe(seconds, stop):
        assert stop.wait(2)
        return SimpleNamespace(
            frames=0, sample_rate=48000, sample_format="f32", channels=2, underrun_frames=0
        )

    monkeypatch.setattr(cli, "probe_silent", fake_probe)
    timer = threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        code = cli.main(["probe-output", "--mute", "30"])
    finally:
        timer.cancel()
    out, err = capsys.readouterr()
    assert code == 0
    assert "無音出力中断" in out
    assert "Traceback" not in out + err
