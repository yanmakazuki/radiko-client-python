"""検索と再生の TUI。描画は状態を読むだけで、通信と復号は呼ばない。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from radiko.catalog import parse_programs
from radiko.domain import (
    STATUS_LABEL,
    BroadcastStatus,
    Filter,
    Program,
    SearchCriteria,
    broadcast_day,
    broadcast_status,
)
from radiko.stream import validate_station
from radiko.text import cluster_width, display_width, graphemes

QUIT_HINT = "終了は q（または Ctrl+C）"
OFFLINE_NOTICE = "オフライン確認: 試験用の番組表です。通信と音声出力はしません"
USAGE = (
    "tui [programsと同じ局・地域・期間・絞り込み]\n"
    "/ キーワード編集 / f 全条件編集 / b 名前付き条件 / Enter 検索確定 / Esc 編集取消 / "
    "j k・上下 選択 / PageUp・PageDown ページ / Home・End 先頭・末尾 / e 取得状況 / r 再取得 / "
    "通常画面のq、またはCtrl+Cで終了。通常画面のEscは出ている通知を消す。このEscでは終了案内を出さない。"
    "通知がなければ「終了は q（または Ctrl+C）」を出し、もう一度Escで消す。停止文・受信待ち・再接続中は案内に替えない\n"
    "Enter TF再生 / v ライブ画面 / l ライブ / g 秒数指定 / Space 一時停止・再開 / s 停止 / 左右 15秒シーク / + - 音量\n"
    "引数省略は前回条件を当日1日に復元。再生は保存済みログインを使用。起動時にセッションを確認し、aで再確認。"
    "I ログイン / O ログアウトはyで確定、n / Esc / qで取消。c チャプター閲覧 / 一覧のr 再取得・Esc 詳細へ。"
    "ライブ画面のEnterはライブ再生、vで戻ります。チャプターのEnterは原点を照合できた枠のみ指定位置から再生します。"
)
COMPACT_HEIGHT = 29
HINT_ITEMS = ["? 操作一覧", "Enter 再生", "Space 一時停止", "q 終了"]
NOTICE_HOLD = 5
DIALOG_AREAS = {"help", "device", "failures", "named", "chapters", "logout"}


def help_text() -> str:
    path = Path(__file__).with_name("help_text.txt")
    if path.exists():
        return path.read_text()
    return files("radiko_client").joinpath("help_text.txt").read_text()


def no_color_enabled(value: str | None) -> bool:
    return value is not None and value != ""


class NoticeLevel:
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @staticmethod
    def rank(level: str) -> int:
        return {NoticeLevel.INFO: 0, NoticeLevel.WARNING: 1, NoticeLevel.ERROR: 2}[level]


@dataclass
class Notice:
    level: str
    text: str
    sticky: bool = False
    created: float = 0.0


@dataclass
class LineEditor:
    value: str = ""
    cursor: int = 0
    byte_limit: int = 1024
    scalar_limit: int | None = None

    @classmethod
    def new(cls, value: str, byte_limit: int) -> LineEditor:
        return cls(value, len(value.encode()), byte_limit)

    def handle(self, key: str) -> None:
        self.cursor = _snap(self.value, min(self.cursor, len(self.value.encode())))
        if key == "ctrl-u":
            self.value = ""
            self.cursor = 0
        elif key == "backspace":
            start = _previous(self.value, self.cursor)
            raw = self.value.encode()
            self.value = (raw[:start] + raw[self.cursor :]).decode()
            self.cursor = start
        elif key == "delete":
            end = _next(self.value, self.cursor)
            raw = self.value.encode()
            self.value = (raw[: self.cursor] + raw[end:]).decode()
        elif key == "left":
            self.cursor = _previous(self.value, self.cursor)
        elif key == "right":
            self.cursor = _next(self.value, self.cursor)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.value.encode())
        elif (
            len(key) == 1
            and key.isprintable()
            and not key.isspace()
            or (len(key) == 1 and key not in {"\n", "\t"} and key.isprintable())
        ):
            self._insert(key)
        self.cursor = _snap(self.value, min(self.cursor, len(self.value.encode())))

    def _insert(self, char: str) -> None:
        if len(self.value.encode()) + len(char.encode()) > self.byte_limit:
            return
        if self.scalar_limit is not None and len(self.value) >= self.scalar_limit:
            return
        raw = self.value.encode()
        self.value = (raw[: self.cursor] + char.encode() + raw[self.cursor :]).decode()
        self.cursor += len(char.encode())

    def view(self, columns: int) -> tuple[str, int]:
        clusters = graphemes(self.value)
        index = 0
        cursor_cluster = 0
        for position, cluster in enumerate(clusters):
            if index >= self.cursor:
                cursor_cluster = position
                break
            index += len(cluster.encode())
        else:
            cursor_cluster = len(clusters)
        if columns <= 0:
            return self.value, 0
        start = 0
        while (
            display_width("".join(clusters[start:cursor_cluster])) >= columns
            and start < cursor_cluster
        ):
            start += 1
        end = cursor_cluster
        while end < len(clusters) and display_width("".join(clusters[start : end + 1])) <= columns:
            end += 1
        text = "".join(clusters[start:end])
        column = display_width("".join(clusters[start:cursor_cluster]))
        if column >= columns:
            column = max(columns - 1, 0)
        return text, column


def _boundaries(value: str) -> list[int]:
    points = [0]
    for cluster in graphemes(value):
        points.append(points[-1] + len(cluster.encode()))
    return points


def _snap(value: str, cursor: int) -> int:
    points = _boundaries(value)
    return min(points, key=lambda point: (abs(point - cursor), point))


def _previous(value: str, cursor: int) -> int:
    points = [point for point in _boundaries(value) if point < cursor]
    return points[-1] if points else 0


def _next(value: str, cursor: int) -> int:
    for point in _boundaries(value):
        if point > cursor:
            return point
    return len(value.encode())


def hint(width: int) -> str:
    chosen = 0
    for count in range(1, len(HINT_ITEMS) + 1):
        candidate = " / ".join(HINT_ITEMS[:count])
        if display_width(candidate) <= width:
            chosen = count
        else:
            break
    return " / ".join(HINT_ITEMS[:chosen])


def playback_line(identity: str, state: str, clock: str, width: int) -> str:
    with_state = identity if not state else f"{identity} {state}"
    full = with_state if not clock else f"{with_state} / {clock}"
    if display_width(full) <= width:
        return full
    if display_width(with_state) <= width:
        return with_state
    if display_width(identity) <= width:
        return identity
    return ""


def wrap_display(text: str, width: int) -> list[str]:
    if text == "":
        return [""]
    if width == 0:
        return [text]
    lines: list[str] = []
    current = ""
    used = 0
    for cluster in graphemes(text):
        cluster_width = display_width(cluster)
        if current and used + cluster_width > width:
            lines.append(current)
            current = ""
            used = 0
        current += cluster
        used += cluster_width
    lines.append(current)
    return lines


def _clock(seconds: float) -> str:
    total = max(int(seconds), 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def truncate_width(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    kept = ""
    for cluster in graphemes(text):
        if display_width(kept + cluster) > width:
            break
        kept += cluster
    return kept


def offline_programs(station: str, day) -> list[Program]:
    validate_station(station)
    stamp = day.strftime("%Y%m%d")
    xml = (
        f'<radiko><stations><station id="{station}"><name>オフライン試験局</name><progs>'
        f'<prog id="offline" ft="{stamp}060000" to="{stamp}070000">'
        "<title>オフライン番組</title><pfm>試験</pfm></prog>"
        "</progs></station></stations></radiko>"
    )
    return parse_programs(xml.encode(), station, day).programs


def split_args(args: list[str]) -> tuple[bool, list[str]]:
    return any(arg == "--offline" for arg in args), [arg for arg in args if arg != "--offline"]


@dataclass
class Screen:
    programs: list[Program]
    now: datetime
    width: int = 80
    height: int = 40
    selected: int = 0
    area: str = "list"
    live: bool = False
    committed: Filter = field(default_factory=Filter.create)
    draft_keyword: LineEditor = field(default_factory=lambda: LineEditor.new("", 1024))
    draft_name: LineEditor = field(default_factory=lambda: LineEditor.new("", 256))
    notice: Notice | None = None
    hidden_warning: Notice | None = None
    volume: int = 25
    paused: bool = False
    playing: Program | None = None
    position: float = 0.0
    generation: int = 0
    applied_generation: int = 0
    fetches: int = 0
    auth: str = "signed-out"
    stop_cause: str | None = None
    output_guide: str | None = None
    device_text: str = ""
    named: dict[str, SearchCriteria] = field(default_factory=dict)
    offline: bool = False
    quit: bool = False
    no_color: bool = False
    clock: float = 0.0

    def visible(self) -> list[Program]:
        return [program for program in self.programs if self.committed.matches(program)]

    def handle(self, key: str) -> None:
        self.clock += 0.05
        self._expire_info()
        if key == "ctrl-c":
            self.quit = True
            return
        if self.area == "keyword":
            self._keyword(key)
            return
        if self.area == "named":
            self._named(key)
            return
        if self.area == "logout":
            self._logout(key)
            return
        if self.area in {"help", "device", "failures", "chapters"}:
            if key in {"q", "esc"} or (self.area == "device" and key == "d"):
                self.area = "list"
            return
        if self.area == "seconds":
            self._seconds(key)
            return
        if key == "esc":
            self._esc()
            return
        self._dismiss_for_key(key)
        if key in {"q"}:
            self.quit = True
            return
        if key in {"j", "down"}:
            self.selected = min(self.selected + 1, max(len(self.visible()) - 1, 0))
        elif key in {"k", "up"}:
            self.selected = max(self.selected - 1, 0)
        elif key == "home":
            self.selected = 0
        elif key == "end":
            self.selected = max(len(self.visible()) - 1, 0)
        elif key == "pageup":
            self.selected = max(self.selected - self._page(), 0)
        elif key == "pagedown":
            self.selected = min(self.selected + self._page(), max(len(self.visible()) - 1, 0))
        elif key == "/":
            self.area = "keyword"
            self.draft_keyword = LineEditor.new(self.committed.keyword or "", 1024)
        elif key == "f":
            self.area = "filter"
        elif key == "b":
            self.area = "named"
        elif key == "?":
            self.area = "help"
        elif key == "e":
            self.area = "failures"
        elif key == "d":
            self.area = "device"
            self.device_text = "取得中…"
        elif key == "v":
            self.live = not self.live
        elif key == "n" and self.area == "list":
            self._on_air()
        elif key == "o" and self.area == "list":
            self._station_draft()
        elif key == " ":
            if self.playing:
                self.paused = not self.paused
                self._set_notice(
                    NoticeLevel.INFO, "一時停止" if self.paused else "再生中", sticky=False
                )
        elif key in {"+", "-"}:
            self.volume = min(100, self.volume + 5) if key == "+" else max(0, self.volume - 5)
        elif key == "s":
            self.playing = None
            self.paused = False
            self._set_notice(NoticeLevel.INFO, "停止しました", sticky=False)
        elif key == "left":
            self._seek(-15)
        elif key == "right":
            self._seek(15)
        elif key == "g":
            self.area = "seconds"
        elif key == "c":
            self.area = "chapters"
        elif key == "I":
            self.area = "login" if not self.offline else "list"
            if self.offline:
                self._set_notice(NoticeLevel.INFO, OFFLINE_NOTICE, sticky=False)
        elif key == "O":
            if not self.offline:
                self.area = "logout"
        elif key == "a":
            if self.offline:
                self._set_notice(NoticeLevel.INFO, OFFLINE_NOTICE, sticky=False)
        elif key == "enter":
            self._enter()
        elif key == "r":
            self._commit_search()

    def keyword_preview(self) -> str | None:
        if self.area != "keyword":
            return None
        try:
            draft = self.committed.with_keyword(self.draft_keyword.value or None)
        except ValueError:
            draft = self.committed
        count = sum(draft.matches(program) for program in self.visible())
        full = f"表示中の一覧で一致 {count} 件（未確定）"
        short = f"一致 {count} 件"
        return full if display_width(full) <= self.width else short

    def render(self) -> list[str]:
        if self.area == "help":
            rows = self._overlay(help_text())
            rows[-1] = truncate_width("q/Esc閉じる", self.width)
            return rows
        if self.area == "device":
            return self._overlay(self.device_text or "出力機器")
        if self.area == "failures":
            return self._overlay("取得状況")
        if self.area == "chapters":
            return self._overlay("チャプター")
        if self.area == "login":
            return self._overlay("ログイン")
        if self.area == "logout":
            return self._overlay("ログアウトしますか（y で確定）")
        compact = self.height <= COMPACT_HEIGHT
        header = 2 if compact else 3
        notice = self.notice.text if self.notice else ""
        notice_lines = wrap_display(notice, max(self.width - 2, 1)) if notice else [""]
        footer_body = max(len(notice_lines), 1) + 2
        body_height = max(self.height - header - footer_body, 1)
        rows = self.visible()
        lines = []
        title = "ライブ" if self.live else "タイムフリー"
        lines.append(
            truncate_width(f"{title}  {OFFLINE_NOTICE if self.offline else '番組一覧'}", self.width)
        )
        if not compact:
            lines.append(
                truncate_width(
                    self.keyword_preview()
                    or f"番組一覧 一致{len(rows)} / 取得{len(self.programs)}枠",
                    self.width,
                )
            )
        for index in range(body_height):
            if index >= len(rows):
                lines.append("")
                continue
            program = rows[index]
            mark = ">" if index == self.selected else " "
            status = STATUS_LABEL[
                broadcast_status(program.key.starts_at, program.ends_at, self.now)
            ]
            label = f"{mark} {program.title} [{status}]"
            lines.append(truncate_width(label, self.width))
        state = "一時停止" if self.paused else "再生中" if self.playing else "停止"
        clock = ""
        if self.playing:
            total = (self.playing.ends_at - self.playing.key.starts_at).total_seconds()
            clock = f"{_clock(self.position)} / {_clock(total)}"
        identity = self.playing.title if self.playing else "停止中"
        if self.stop_cause == "auth" and self.auth == "signed-out":
            identity = "停止中：認証期限切れ（I で再ログイン）"
        elif self.stop_cause == "auth":
            identity = "停止中：認証期限切れ（a で再確認）"
        elif self.stop_cause == "reconnect":
            identity = "停止中：再接続できませんでした（番組を選び直してください）"
        lines.append(
            truncate_width(
                playback_line(identity, state if self.playing else "", clock, self.width),
                self.width,
            )
        )
        prefix = (
            {"error": "✕ ", "warning": "! ", "info": ""}[self.notice.level]
            if self.notice and not self.no_color
            else ""
        )
        if self.no_color:
            prefix = ""
        shown = prefix + (notice_lines[0] if notice else "")
        lines.append(truncate_width(shown, self.width))
        lines.append(truncate_width(hint(self.width), self.width))
        return lines[: self.height]

    def cells(self) -> list[list[str]]:
        """幅と高さに揃えたセル。全角は2列、2列目は空にする。書記素は分けない。"""
        grid = []
        for line in self.render():
            row: list[str] = []
            for cluster in graphemes(line):
                row.append(cluster)
                if cluster_width(cluster) == 2 and len(row) < self.width:
                    row.append("")
                if len(row) >= self.width:
                    break
            row.extend(" " for _ in range(self.width - len(row)))
            grid.append(row[: self.width])
        blank = [" "] * self.width
        grid.extend(blank[:] for _ in range(self.height - len(grid)))
        return grid[: self.height]

    def _overlay(self, text: str) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [""]:
            lines.extend(wrap_display(paragraph, max(self.width, 1)))
        shown = [truncate_width(line, self.width) for line in lines[: self.height]]
        shown.extend("" for _ in range(self.height - len(shown)))
        return shown

    def publish(self, generation: int, programs: list[Program]) -> bool:
        if generation != self.generation:
            return False
        self.programs = programs
        self.applied_generation = generation
        self.selected = min(self.selected, max(len(self.visible()) - 1, 0))
        return True

    def _commit_search(self) -> None:
        self.generation += 1
        self.fetches += 1
        self.area = "list"

    def _enter(self) -> None:
        if self.area == "filter":
            self._commit_search()
            return
        rows = self.visible()
        if not rows:
            self._set_notice(NoticeLevel.INFO, "移動できる番組がありません", sticky=False)
            return
        program = rows[min(self.selected, len(rows) - 1)]
        status = broadcast_status(program.key.starts_at, program.ends_at, self.now)
        if status == BroadcastStatus.UPCOMING:
            self._set_notice(NoticeLevel.WARNING, "放送前のため再生できません", sticky=True)
            return
        if status == BroadcastStatus.FINISHED and not self.live:
            self.playing = program
            self.position = 0.0
            self._set_notice(NoticeLevel.INFO, "再生準備中", sticky=False)
            return
        self.playing = program
        self.position = 0.0
        self.paused = False

    def _on_air(self) -> None:
        rows = self.visible()
        if not rows:
            self._set_notice(NoticeLevel.INFO, "移動できる番組がありません", sticky=False)
            return
        for index, program in enumerate(rows[self.selected :], start=self.selected):
            if (
                broadcast_status(program.key.starts_at, program.ends_at, self.now)
                == BroadcastStatus.ON_AIR
            ):
                self.selected = index
                return
        best = min(
            range(len(rows)),
            key=lambda index: (abs((rows[index].key.starts_at - self.now).total_seconds()), index),
        )
        self.selected = best
        self._set_notice(NoticeLevel.INFO, "現在時刻に近い番組を選択しました", sticky=False)

    def _station_draft(self) -> None:
        rows = self.visible()
        if not rows:
            self._set_notice(NoticeLevel.INFO, "局で絞るには番組を選択してください", sticky=False)
            return
        program = rows[min(self.selected, len(rows) - 1)]
        self.committed = Filter.create(
            self.committed.program_genres,
            self.committed.personality_genres,
            self.committed.unclassified_program,
            self.committed.unclassified_personality,
            self.committed.keyword,
        )
        self.area = "filter"
        self.station_scope = program.key.station_id

    def _seek(self, delta: int) -> None:
        if not self.playing:
            return
        total = (self.playing.ends_at - self.playing.key.starts_at).total_seconds()
        target = self.position + delta
        if target < 0 or target >= total:
            self._set_notice(NoticeLevel.WARNING, "シーク不可", sticky=True)
            return
        self.position = target

    def _keyword(self, key: str) -> None:
        if key == "esc":
            self.area = "list"
            self._dismiss_error_only()
            return
        if key == "enter":
            try:
                self.committed = self.committed.with_keyword(self.draft_keyword.value or None)
            except ValueError as error:
                self._set_notice(NoticeLevel.ERROR, str(error), sticky=True)
                return
            self._dismiss_error_only()
            self._commit_search()
            return
        if self.notice and self.notice.level == NoticeLevel.ERROR:
            return
        if len(key) == 1 or key in {
            "backspace",
            "delete",
            "left",
            "right",
            "home",
            "end",
            "ctrl-u",
        }:
            self.draft_keyword.handle(key)

    def _named(self, key: str) -> None:
        if key in {"q", "esc"}:
            self.area = "list"
            return
        if key == "enter":
            name = self.draft_name.value
            if name in self.named:
                criteria = self.named[name]
                self.committed = criteria.filter
                self._set_notice(NoticeLevel.INFO, f"「{name}」を呼び出しました", sticky=False)
            self.area = "list"
            return
        self.draft_name.handle(key)

    def _logout(self, key: str) -> None:
        if key == "y":
            self.auth = "signed-out"
            self.area = "list"
            self._set_notice(NoticeLevel.INFO, "ログアウトしました", sticky=False)
            return
        if key in {"n", "esc", "q"}:
            self.area = "list"

    def _seconds(self, key: str) -> None:
        if key == "esc":
            self.area = "list"
            return
        if key == "enter":
            self.area = "list"
            return
        if key.isdigit():
            return

    def _esc(self) -> None:
        if self.output_guide:
            self.output_guide = None
        if self.notice is None:
            self._set_notice(NoticeLevel.INFO, QUIT_HINT, sticky=False)
            return
        if self.notice.text == QUIT_HINT:
            self.notice = None
            return
        self.notice = None
        if self.hidden_warning:
            self.notice = self.hidden_warning
            self.hidden_warning = None

    def _dismiss_for_key(self, key: str) -> None:
        if self.notice is None:
            return
        if self.notice.level == NoticeLevel.ERROR:
            return
        if self.notice.level == NoticeLevel.WARNING or not self.notice.sticky:
            self.notice = self.hidden_warning
            self.hidden_warning = None

    def _dismiss_error_only(self) -> None:
        if self.notice and self.notice.level == NoticeLevel.ERROR:
            self.notice = self.hidden_warning
            self.hidden_warning = None

    def _expire_info(self) -> None:
        if self.notice and self.notice.level == NoticeLevel.INFO and not self.notice.sticky:
            if self.clock - self.notice.created >= NOTICE_HOLD:
                self.notice = None

    def _set_notice(self, level: str, text: str, sticky: bool) -> None:
        incoming = Notice(level, text, sticky, self.clock)
        if self.notice and NoticeLevel.rank(self.notice.level) > NoticeLevel.rank(level):
            if level == NoticeLevel.WARNING:
                self.hidden_warning = incoming
            return
        if self.notice and self.notice.level == NoticeLevel.WARNING and level == NoticeLevel.ERROR:
            self.hidden_warning = self.notice
        self.notice = incoming

    def _page(self) -> int:
        return max(self.height - 6, 1)


def run(args: list[str], data_dir: Path | None) -> None:
    if args == ["--help"] or "--help" in args:
        from radiko_client.io import stdout_ln

        stdout_ln(USAGE)
        return
    offline, rest = split_args(args)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("対話端末で実行してください")
    _ = rest, data_dir
    screen = Screen([], datetime.now().astimezone(), offline=offline)
    if offline:
        day = broadcast_day(screen.now)
        try:
            screen.programs = offline_programs("TBS", day)
        except Exception:
            screen.programs = []
        screen.notice = Notice(NoticeLevel.INFO, OFFLINE_NOTICE, False, 0)
    _terminal_loop(screen)


def _terminal_loop(screen: Screen) -> None:
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?1049h\x1b[2J")
        sys.stdout.flush()
        while not screen.quit:
            sys.stdout.write("\x1b[H")
            for line in screen.render():
                sys.stdout.write(line + "\x1b[K\n")
            sys.stdout.flush()
            key = _read_key()
            screen.handle(key)
    finally:
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _read_key() -> str:
    char = sys.stdin.read(1)
    if char == "\x03":
        return "ctrl-c"
    if char == "\x1b":
        rest = sys.stdin.read(1)
        if rest == "[":
            code = sys.stdin.read(1)
            return {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}.get(
                code, "esc"
            )
        return "esc"
    if char in {"\r", "\n"}:
        return "enter"
    if char == "\x7f":
        return "backspace"
    if char == " ":
        return " "
    return char
