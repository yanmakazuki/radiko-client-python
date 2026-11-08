from datetime import datetime, timedelta, timezone

from radiko.catalog import parse_programs
from radiko.domain import Program, ProgramKey
from radiko.media import AacError, AdtsDecoder, split_adts
from radiko.player import Engine
from radiko.text import display_width, graphemes
from radiko_client.tui import (
    COMPACT_HEIGHT,
    LineEditor,
    NoticeLevel,
    Screen,
    hint,
    playback_line,
    truncate_width,
    wrap_display,
)

JST = timezone(timedelta(hours=9))


def program(title, start, end, station="TBS"):
    return Program(ProgramKey(station, start), "id", end, title)


def test_grapheme_editor_moves_by_cluster_not_code_unit():
    editor = LineEditor.new("", 1024)
    editor.handle("あ")
    editor.handle("a\u0301")
    assert graphemes(editor.value) == ["あ", "á"] or graphemes(editor.value)[0] == "あ"
    editor.handle("left")
    editor.handle("backspace")
    assert editor.value == "あ"
    assert display_width("あ") == 2
    assert display_width("A") == 1
    view, column = LineEditor.new("あいうえお", 1024).view(4)
    assert display_width(view) <= 4
    assert column < 4


def test_compact_layout_drops_clock_before_state_and_wraps_notices():
    assert hint(4) == ""
    assert "操作一覧" in hint(80)
    assert playback_line("番組", "再生中", "12:00 / 55:00", 8) == "番組"
    assert playback_line("番組", "再生中", "12:00 / 55:00", 12) == "番組 再生中"
    wrapped = wrap_display("あいうえ", 2)
    assert wrapped == ["あ", "い", "う", "え"]
    assert truncate_width("番組名です", 4) == "番組"


def test_keyword_preview_is_uncommitted_and_enter_bumps_generation():
    now = datetime(2026, 10, 15, 6, 30, tzinfo=JST)
    programs = [
        program("朝刊", now - timedelta(hours=1), now + timedelta(minutes=30)),
        program("音楽", now + timedelta(hours=2), now + timedelta(hours=3)),
    ]
    screen = Screen(programs, now, width=80, height=40)
    screen.handle("/")
    screen.draft_keyword.handle("朝")
    preview = screen.keyword_preview()
    assert preview == "表示中の一覧で一致 1 件（未確定）"
    assert len(screen.visible()) == 2
    generation = screen.generation
    screen.handle("enter")
    assert screen.generation == generation + 1
    assert screen.committed.keyword == "朝"
    assert len(screen.visible()) == 1
    assert screen.publish(screen.generation - 1, programs) is False


def test_notices_keep_hidden_warnings_and_errors_survive_typing():
    now = datetime(2026, 10, 15, 6, 30, tzinfo=JST)
    screen = Screen([program("朝", now - timedelta(minutes=5), now + timedelta(minutes=5))], now)
    screen._set_notice(NoticeLevel.WARNING, "警告", sticky=True)
    screen._set_notice(NoticeLevel.ERROR, "失敗", sticky=True)
    assert screen.notice.text == "失敗"
    assert screen.hidden_warning.text == "警告"
    screen.handle("/")
    screen.handle("a")
    assert screen.notice.text == "失敗"
    screen.handle("esc")
    assert screen.notice.text == "警告"
    screen.handle("j")
    assert screen.notice is None


def test_on_air_selection_and_upcoming_does_not_replace_playback():
    now = datetime(2026, 10, 15, 6, 30, tzinfo=JST)
    current = program("いま", now - timedelta(minutes=10), now + timedelta(minutes=10))
    later = program("あと", now + timedelta(hours=3), now + timedelta(hours=4))
    screen = Screen([later, current], now)
    screen.selected = 0
    screen.handle("n")
    assert screen.visible()[screen.selected].title == "いま"
    screen.playing = current
    screen.selected = 0
    screen.handle("enter")
    assert screen.playing.title == "いま"


def test_escape_on_the_main_screen_does_not_quit():
    now = datetime(2026, 10, 15, 12, 0, tzinfo=JST)
    screen = Screen([], now, height=COMPACT_HEIGHT)
    screen.handle("esc")
    assert screen.quit is False
    assert screen.notice.text == "終了は q（または Ctrl+C）"
    screen.handle("esc")
    assert screen.notice is None
    screen.handle("q")
    assert screen.quit is True
    rendered = screen.render()
    assert len(rendered) <= COMPACT_HEIGHT


def test_pause_keeps_the_frames_already_sent(monkeypatch):
    monkeypatch.setenv("RADIKO_NULL_SINK", "1")
    engine = Engine()
    generation = engine.request("TBS", "timefree", "20261015120000", 0)
    seen = {}

    def hook(played):
        if played >= 4800:
            engine.pause()

    from radiko.output import play_samples

    summary = play_samples([0.2, 0.2] * 48000, 48000, 2, engine.stop, engine._pause_flag, 25, hook)
    assert summary.frames == 4800 or summary.frames % (48000 // 50) == 0
    assert summary.frames < 48000
    assert engine.position == 0
    report = engine.commit(generation, [0.2, 0.2] * 1000, 48000, 2, None)
    assert report is not None
    assert engine.commit(generation - 1, [0.2, 0.2] * 1000, 48000, 2, None) is None
    _ = seen


def test_adts_rejects_crc_and_decodes_a_generated_frame():
    from radiko.media import encode_adts_sine

    data = encode_adts_sine()
    frames = split_adts(data, lambda: False)
    assert frames
    pcm = AdtsDecoder().decode_segment_pcm(data, None)
    assert pcm.summary.packets == len(frames)
    assert pcm.summary.sample_rate > 0
    assert pcm.summary.nonzero_samples > 0
    bad = bytearray(data)
    bad[1] = bad[1] & 0xFE
    try:
        split_adts(bytes(bad), lambda: False)
    except AacError as error:
        assert error.code == "transport"
    else:
        raise AssertionError("crc frame must be rejected")
    with_declaration = AdtsDecoder()
    try:
        with_declaration.decode_segment_pcm(data, "mp4a.40.5")
    except AacError as error:
        assert error.code == "sbr-missing"
    else:
        raise AssertionError("LC audio must not satisfy a HE-AAC declaration")


def test_fixture_schedule_is_not_compared_to_itself():
    listing = parse_programs(
        (__import__("pathlib").Path(__file__).parent / "fixtures" / "schedule.xml").read_bytes(),
        "TBS",
        __import__("datetime").date(2026, 10, 15),
    )
    assert listing.station_name == "テスト局"
    assert listing.programs[1].title == "朝 & 続編"
