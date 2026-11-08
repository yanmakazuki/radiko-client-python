import json
import os
from pathlib import Path

from radiko_client.cli import command_line, main
from radiko_client.io import BROKEN_PIPE_STATUS

FIXTURE = Path(__file__).parent / "fixtures" / "chapters.json"


def run(args, capsys):
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_help_and_unknown_arguments(capsys):
    code, out, err = run(["--help"], capsys)
    assert code == 0
    assert "inspect-chapters" in out
    assert err == ""
    code, out, err = run(["nope"], capsys)
    assert code == 1
    assert out == ""
    assert err.startswith("エラー: 引数が不正です")


def test_session_logout_and_login_do_not_create_a_directory(tmp_path, capsys):
    data = tmp_path / "data"
    code, out, err = run(["--data-dir", str(data), "session"], capsys)
    assert code == 0
    assert "未ログイン" in out
    assert not data.exists()
    code, out, err = run(["logout", "--data-dir", str(data)], capsys)
    assert code == 0
    assert "削除しました" in out
    assert not data.exists()
    code, out, err = run(["login", "--data-dir", str(data)], capsys)
    assert code == 1
    assert "対話端末" in err
    assert "synthetic-password" not in err
    code, out, err = run(
        ["login", "--password", "synthetic-password", "--data-dir", str(data)], capsys
    )
    assert code == 1
    assert "synthetic-password" not in err
    assert not data.exists()


def test_corrupt_session_is_preserved(tmp_path, capsys):
    os.chmod(tmp_path, 0o700)
    path = tmp_path / "session.json"
    path.write_bytes(b"corrupt-synthetic-session")
    os.chmod(path, 0o600)
    code, out, err = run(["session", "--data-dir", str(tmp_path)], capsys)
    assert code == 0
    assert "破損" in err
    assert path.read_bytes() == b"corrupt-synthetic-session"
    assert main(["logout", "--data-dir", str(tmp_path)]) == 0
    assert not path.exists()


def test_data_dir_and_inspect_chapters(capsys):
    assert main(["session", "--data-dir"]) == 1
    assert main(["session", "--data-dir", "a", "--data-dir", "b"]) == 1
    code, out, err = run(["inspect-chapters", str(FIXTURE), "90", "0"], capsys)
    assert code == 0, err
    assert "オープニング" in out
    assert "不正項目: 1 / 除外した楽曲: 1" in out


def test_programs_and_output_and_playback_reject_bad_arguments(tmp_path, capsys):
    data = tmp_path / "absent"
    for args in [
        ["programs"],
        ["programs", "../TBS"],
        ["programs", "TBS", "20260230"],
        ["programs", "TBS", "--json", "--json"],
        ["programs", "--areas"],
        ["programs", "--areas", "JP0"],
        ["programs", "--areas", "JP01"],
        ["programs", "--all", "--areas", "JP13"],
        ["programs", "TBS", "20261015", "20261014"],
        ["programs", "--all", "20260915", "20261015"],
        ["programs", "TBS", "--keyword", ""],
        ["programs", "TBS", "--genre", "A,"],
        ["output-info", "0"],
        ["output-info", "7999"],
        ["output-info", "192001"],
        ["output-info", "bad"],
        ["probe-output", "0"],
        ["probe-output", "11"],
        ["probe-output", "--mute", "0"],
        ["probe-output", "--mute", "1801"],
        ["play-sample"],
        ["play-sample", "../TBS"],
        ["play-live", "../TBS"],
        ["play-live", "TBS", "--seconds", "0"],
        ["play-timefree", "TBS", "20990101000000"],
        ["chapters"],
        ["chapters", "../TBS", "20261015120000"],
        ["chapters", "TBS", "20260230000000"],
    ]:
        code, out, err = run(["--data-dir", str(data), *args], capsys)
        assert code == 1, args
    assert not data.exists()
    code, out, err = run(["programs", "--help"], capsys)
    assert code == 0
    for flag in ["--areas", "--all", "--genre", "--personality", "--keyword", "--classifications"]:
        assert flag in out
    assert "読み取りのみ" in run(["output-info", "--help"], capsys)[1]
    assert "--mute" in run(["probe-output", "--help"], capsys)[1]
    assert run(["play-live", "--help"], capsys)[0] == 0
    code, out, err = run(["--data-dir", str(data), "chapters", "--help"], capsys)
    assert "chapters STATION" in out
    assert not data.exists()


def test_named_searches_round_trip_without_network(tmp_path, capsys):
    data = tmp_path / "criteria"
    code, out, err = run(["--data-dir", str(data), "searches", "list"], capsys)
    assert code == 0
    assert out == ""
    assert not data.exists()
    code, out, err = run(
        [
            "--data-dir",
            str(data),
            "searches",
            "save",
            "生活",
            "TBS",
            "20261014",
            "20261015",
            "--keyword",
            "生活",
        ],
        capsys,
    )
    assert code == 0, err
    code, out, err = run(["--data-dir", str(data), "searches", "show", "生活"], capsys)
    shown = json.loads(out)
    assert shown["start"] == "20261014"
    assert shown["end"] == "20261015"
    assert shown["scope"]["value"] == "TBS"
    assert shown["filter"]["keyword"] == "生活"
    assert "生活" in run(["--data-dir", str(data), "searches", "list"], capsys)[1]
    code, out, err = run(["--data-dir", str(data), "searches", "restore"], capsys)
    restored = json.loads(out)
    assert restored["scope"]["kind"] == "nationwide"
    assert restored["start"] == restored["end"]
    assert main(["--data-dir", str(data), "searches", "delete", "生活"]) == 0
    assert main(["--data-dir", str(data), "searches", "show", "生活"]) == 1
    assert not (data / "session.json").exists()


def test_saved_search_does_not_overwrite_corrupt_storage(tmp_path, capsys):
    data = tmp_path / "data"
    for args in [
        ["searches", "save", "name", "../TBS"],
        ["searches", "save", "name", "TBS", "20260230"],
        ["programs", "--saved", "missing"],
        ["programs", "--saved"],
        ["programs", "--restore", "TBS"],
    ]:
        assert main(["--data-dir", str(data), *args]) == 1
    assert not data.exists()
    assert main(["--data-dir", str(data), "searches", "save", "one", "TBS"]) == 0
    target = data / "searches.json"
    target.write_bytes(b"broken")
    code, out, err = run(["--data-dir", str(data), "searches", "restore"], capsys)
    assert code == 0
    assert "復元できません" in err
    assert target.read_bytes() == b"broken"
    assert main(["--data-dir", str(data), "searches", "save", "two", "TBS"]) == 1
    assert target.read_bytes() == b"broken"


def test_tui_help_hides_offline_and_non_tty_is_rejected(tmp_path, capsys):
    data = tmp_path / "data"
    for args in [
        ["--help"],
        ["tui", "--help"],
        ["tui", "--offline", "--help"],
        ["tui", "--help", "--offline"],
    ]:
        code, out, err = run(args, capsys)
        assert code == 0, args
        assert "--offline" not in out
        if "tui" in args:
            assert "g 秒数指定" in out
    code, out, err = run(["--data-dir", str(data), "tui"], capsys)
    assert code == 1
    assert "対話端末" in err
    assert not data.exists()


def test_probe_requires_a_session_before_any_directory_is_created(tmp_path, capsys):
    data = tmp_path / "data"
    code, out, err = run(["--data-dir", str(data), "probe-live", "TBS"], capsys)
    assert code == 1
    assert "未ログイン" in err
    code, out, err = run(
        ["--data-dir", str(data), "probe-timefree", "TBS", "20990101000000"], capsys
    )
    assert "放送前" in err
    code, out, err = run(["--data-dir", str(data), "play-live", "TBS"], capsys)
    assert "未ログイン" in err
    assert not data.exists()


def test_interactive_messages_are_not_reprinted_and_header_precedes_the_result(
    tmp_path, capsys, monkeypatch
):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from radiko.prepare import PlayReport
    from radiko.storage import MemberSession, Membership, SessionStore
    from radiko_client import cli

    data = tmp_path / "data"
    SessionStore(data).save(MemberSession("synthetic-token"))
    program = SimpleNamespace(
        title="合成番組",
        start=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc),
        end=datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc),
    )

    def fake_play(*_args, **kwargs):
        kwargs["on_ready"](program)
        notice = kwargs.get("on_notice") or cli.stderr_ln
        notice("合成メッセージ")
        return PlayReport(
            "limit",
            1,
            48000,
            2,
            1,
            0,
            program.title,
            "TBS",
            "Live",
            program.start,
            program.end,
            ["合成メッセージ"],
        )

    import radiko.prepare

    monkeypatch.setattr(radiko.prepare, "play_station", fake_play)
    monkeypatch.setattr(cli, "_membership", lambda _session, _stop: Membership(True, True))
    code, out, err = run(["--data-dir", str(data), "play-live", "TBS"], capsys)
    assert code == 0, err
    assert err.count("合成メッセージ") == 1
    assert out.index("合成番組") < out.index("指定時間で停止しました")


def test_interactive_invalid_switch_uses_the_single_rust_guidance():
    from radiko.interactive import InteractiveControl

    control = InteractiveControl(3600, "live", "TBS")
    control.handle("timefree TBS")
    assert control.messages == [
        "live 局ID / timefree 局ID YYYYMMDDHHmmss [--offset 秒数] を指定してください"
    ]


def test_interactive_commands_reject_seconds_on_a_switch():
    now = __import__("datetime").datetime(
        2026, 10, 16, 12, 0, tzinfo=__import__("datetime").timezone.utc
    )
    assert command_line("volume 25", now) == ("volume", 25)
    assert command_line("+15", now) == "+15"
    try:
        command_line("live TBS --seconds 30", now)
    except RuntimeError as error:
        assert "--seconds" in str(error)
    else:
        raise AssertionError("expected rejection")


def test_closed_pipe_status_constant():
    assert BROKEN_PIPE_STATUS == 141
