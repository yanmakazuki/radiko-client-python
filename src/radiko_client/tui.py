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

DEVICE_NOTE = "出力機器は OS の既定に従います。アプリ内では選べません"
DEVICE_GUIDANCE = "OSのサウンド設定を確認し、d で閉じてから Enter で再生し直してください"
DEVICE_NONE = "既定の音声出力機器がありません"
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
    device_status: str = "idle"
    device_generation: int = 0
    device_report: object | None = None
    device_error: str = ""
    device_box: object | None = None
    device_probe: object | None = None
    station_id: str = "TBS"
    fetch_programs: bool = False
    area_failures: list[tuple[str, str]] = field(default_factory=list)
    schedule_failures: list[tuple[str, str, str]] = field(default_factory=list)
    invalid_count: int = 0
    named: dict[str, SearchCriteria] = field(default_factory=dict)
    offline: bool = False
    quit: bool = False
    revision: int = 0
    no_color: bool = False
    clock: float = 0.0
    mail: str = ""
    password: str = ""
    password_focus: bool = False
    login_request: tuple[str, str] | None = None
    chapters: list[tuple[float, float, str]] = field(default_factory=list)
    chapter_index: int = 0
    chapter_program: Program | None = None
    play_request: tuple[Program, float] | None = None
    played_frames: int = 0
    fetch_chapters: bool = False
    logout_request: bool = False
    seconds_text: str = ""
    data_dir: Path | None = None

    def visible(self) -> list[Program]:
        return [program for program in self.programs if self.committed.matches(program)]

    def handle(self, key: str) -> None:
        self.clock += 0.05
        self._expire_info()
        if key == "ctrl-c":
            self.quit = True
            return
        if self.area == "login":
            self._login_key(key)
            return
        if self.area == "chapters":
            self._chapters(key)
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
        if self.area in {"help", "device", "failures"}:
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
            self._request_device()
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
            if self.offline:
                self._set_notice(NoticeLevel.INFO, OFFLINE_NOTICE, sticky=False)
            else:
                self.fetch_chapters = True
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
            return self._framed("出力機器", self._device_body())
        if self.area == "failures":
            return self._framed("取得状況・未取得範囲", self._failures_body())
        if self.area == "chapters":
            return self._overlay("チャプター")
        if self.area == "login":
            return self._framed("ログイン（パスワード非表示）", self._login_body())
        if self.area == "chapters":
            return self._framed("チャプター", self._chapter_body())
        if self.area == "logout":
            return self._overlay("ログアウトしますか（y で確定）")
        compact = self.height <= COMPACT_HEIGHT
        rows = self.visible()
        lines = self._header(compact, rows)
        title = (
            "番組一覧"
            if compact
            else self.keyword_preview() or f"番組一覧 一致{len(rows)} / 取得{len(self.programs)}枠"
        )
        lines.append(truncate_width(title, self.width))
        footer = self._footer(compact)
        body_height = max(self.height - len(lines) - len(footer), 0)
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
        lines.extend(footer)
        lines = lines[: self.height]
        lines.extend("" for _ in range(self.height - len(lines)))
        return lines

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

    def _scope(self) -> str:
        rows = self.visible()
        if not rows:
            return "指定なし"
        return rows[0].key.station_id

    def _mode_label(self) -> str:
        mode = "ライブ（放送中のみ）" if self.live else "タイムフリー"
        if self.offline:
            return f"{mode} / オフライン確認"
        return mode

    def _header(self, compact: bool, _rows: list[Program]) -> list[str]:
        scope = self._scope()
        day = broadcast_day(self.now)
        if compact:
            return [
                truncate_width(f"{self._mode_label()} / {scope} / {day}〜{day}", self.width),
                truncate_width(OFFLINE_NOTICE if self.offline else self.auth, self.width),
            ]
        lines = [
            "┌ radiko-client",
            f"│ {self._mode_label()} / {scope} / {day}〜{day}（日本時間・05:00区切り）",
            "│ キーワード: 指定なし",
            "│ 番組ジャンル: 指定なし / 出演者分類: 指定なし",
            f"│ 認証: {self.auth}",
            "│ " + (OFFLINE_NOTICE if self.offline else ""),
            "└",
        ]
        return [truncate_width(line, self.width) for line in lines]

    def _footer(self, compact: bool) -> list[str]:
        if self.playing:
            mode = "ライブ" if self.live else "タイムフリー"
            state = "一時停止" if self.paused else "再生中"
            total = (self.playing.ends_at - self.playing.key.starts_at).total_seconds()
            clock = f"{_clock(self.position)} / {_clock(total)}"
            identity = f"再生中: [{self.playing.key.station_id} / {mode}]"
            play = playback_line(identity, state, clock, self.width)
            if (
                not compact
                and display_width(play) + display_width(f" / 音量{self.volume}%") <= self.width
            ):
                play = f"{play} / 音量{self.volume}%"
        else:
            play = "再生中: なし" if compact else f"再生中: なし / 音量{self.volume}%"
        notice = ""
        if self.notice:
            mark = (
                ""
                if self.no_color
                else {"error": "✕ ", "warning": "! ", "info": ""}[self.notice.level]
            )
            notice = mark + self.notice.text
        controls = hint(self.width) if compact else "e 取得状況 / ? 操作一覧 / Enter 再生 / q 終了"
        return [
            truncate_width(play, self.width),
            truncate_width(notice, self.width),
            truncate_width(controls, self.width),
        ]

    def notice_color(self) -> str | None:
        if self.no_color or self.notice is None or self.notice.level == NoticeLevel.INFO:
            return None
        return "red" if self.notice.level == NoticeLevel.ERROR else "yellow"

    def _login_body(self) -> str:
        hidden = "未入力" if self.password == "" else "入力済み（非表示）"
        mail_mark = " " if self.password_focus else ">"
        password_mark = ">" if self.password_focus else " "
        return f"{mail_mark} メールアドレス: {self.mail}\n{password_mark} パスワード: {hidden}"

    def _chapter_body(self) -> str:
        if not self.chapters:
            return "チャプター未提供"
        lines = []
        for index, (begin, finish, title) in enumerate(self.chapters):
            mark = ">" if index == self.chapter_index else " "
            lines.append(f"{mark} {begin:.3f}–{finish:.3f}秒 {title}")
        return "\n".join(lines)

    def _framed(self, title: str, body: str) -> list[str]:
        lines = [truncate_width(f"┌ {title}", self.width)]
        for paragraph in body.splitlines() or [""]:
            wrapped = wrap_display(paragraph, max(self.width - 2, 1))
            lines.extend(truncate_width(f"│ {line}", self.width) for line in wrapped)
        lines.append(truncate_width("└", self.width))
        lines = lines[: self.height]
        lines.extend("" for _ in range(self.height - len(lines)))
        return lines

    def _login_key(self, key: str) -> None:
        if key == "esc":
            self.password = ""
            self.area = "list"
            return
        if key == "tab":
            self.password_focus = not self.password_focus
            return
        if key == "enter" and not self.password_focus:
            self.password_focus = True
            return
        if key == "enter":
            if not self.mail.strip() or self.password == "":
                self._set_notice(
                    NoticeLevel.ERROR, "メールアドレスとパスワードを入力してください", True
                )
                return
            self.login_request = (self.mail, self.password)
            self.password = ""
            self.mail = ""
            self.area = "list"
            return
        if key == "backspace":
            if self.password_focus:
                self.password = self.password[:-1]
            else:
                self.mail = self.mail[:-1]
            return
        if key == "ctrl-u":
            if self.password_focus:
                self.password = ""
            else:
                self.mail = ""
            return
        if len(key) == 1 and key.isprintable() and not self.offline:
            limit = 4096 if self.password_focus else 1024
            current = self.password if self.password_focus else self.mail
            if len(current.encode()) + len(key.encode()) <= limit:
                if self.password_focus:
                    self.password += key
                else:
                    self.mail += key

    def _chapters(self, key: str) -> None:
        if key in {"q", "esc"}:
            self.area = "list"
            return
        if key in {"j", "down"} and self.chapters:
            self.chapter_index = min(self.chapter_index + 1, len(self.chapters) - 1)
            return
        if key in {"k", "up"}:
            self.chapter_index = max(self.chapter_index - 1, 0)
            return
        if key == "r" and not self.offline:
            self.fetch_chapters = True
            return
        if key != "enter" or not self.chapters or self.chapter_program is None:
            if key == "enter":
                self._set_notice(NoticeLevel.WARNING, "再生できるチャプターがありません", True)
            return
        begin, _finish, title = self.chapters[self.chapter_index]
        self.position = begin
        self.playing = self.chapter_program
        self.play_request = (self.chapter_program, begin)
        self._set_notice(NoticeLevel.INFO, f"{title} から再生します", False)

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
        if not self.offline:
            self.fetch_programs = True

    def _request_device(self) -> None:
        import queue
        import threading

        self.device_generation += 1
        generation = self.device_generation
        self.device_status = "loading"
        self.device_text = "取得中…"
        box: queue.Queue = queue.Queue(1)
        self.device_box = box
        probe = self.device_probe

        def work() -> None:
            from radiko.output import OutputError, diagnose_default_output

            try:
                report = (probe or diagnose_default_output)(48000)
                box.put((generation, report, ""))
            except OutputError as error:
                sentence = (
                    DEVICE_NONE if error.code == "none" else f"出力機器を確認できません。{error}"
                )
                box.put((generation, None, sentence))

        threading.Thread(target=work, daemon=True).start()

    def poll_device(self) -> None:
        box = self.device_box
        if box is None:
            return
        import queue

        try:
            generation, report, sentence = box.get_nowait()
        except queue.Empty:
            return
        if generation != self.device_generation:
            return
        self.device_box = None
        self.revision += 1
        if report is None:
            self.device_status = "failed"
            self.device_error = sentence
            self.device_text = sentence
            return
        self.device_status = "ready"
        self.device_report = report
        self.device_error = ""
        self.device_text = f"{report.host} / {report.name}"

    def _device_body(self) -> str:
        if self.device_status == "loading":
            lines = ["取得中…"]
        elif self.device_status == "failed":
            mark = "" if self.no_color else "✕ "
            lines = [f"{mark}{self.device_error}", DEVICE_GUIDANCE]
        elif self.device_status == "ready" and self.device_report is not None:
            report = self.device_report
            mono = "対応" if report.f32_mono else "非対応"
            stereo = "対応" if report.f32_stereo else "非対応"
            lines = [
                f"ホスト: {report.host}",
                f"機器: {report.name}",
                f"f32 mono {report.sample_rate}Hz: {mono}",
                f"f32 stereo {report.sample_rate}Hz: {stereo}",
                "最後の出力エラー: なし",
            ]
        else:
            lines = ["機器: 未取得"]
        lines.extend([DEVICE_NOTE, "q / Esc / d 閉じる"])
        return "\n".join(lines)

    def _failures_body(self) -> str:
        if self.fetches == 0:
            status = "未取得"
        elif self.area_failures or self.schedule_failures:
            status = "部分取得・未取得あり"
        else:
            status = "取得完了"
        day = broadcast_day(self.now)
        lines = [
            f"{day}〜{day} JST / {status}",
            f"地域失敗{len(self.area_failures)} / 局・日失敗{len(self.schedule_failures)} / 不正枠{self.invalid_count}",
            "不正枠は番組一覧から除外します。今回の取得だけを表示します。",
        ]
        for target, error in self.area_failures:
            lines.append(f"地域 {target}  / {error}")
        for target, when, error in self.schedule_failures:
            lines.append(f"番組表 {target} {when} / {error}")
        if self.invalid_count:
            lines.append(
                f"不正枠除外 {self.station_id} {day} / {self.invalid_count}件（正常な枠は表示）"
            )
        lines.append("再生中: なし")
        lines.append("上下・ページ・Home/End 選択 / e / q / Esc 閉じる / Ctrl+C 終了")
        return "\n".join(lines)

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
            self.area = "list"
            if self.offline:
                self._set_notice(NoticeLevel.INFO, "オフライン確認ではログアウトしません", False)
                return
            self.logout_request = True
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


def perform_pending(screen: Screen) -> None:
    """ログインとチャプター取得と再生。描画の外で、保存済みセッションを使う。"""
    if screen.offline:
        screen.login_request = None
        screen.fetch_chapters = False
        screen.play_request = None
        screen.logout_request = False
        return
    if screen.fetch_programs:
        screen.fetch_programs = False
        _fetch_programs(screen)
    if screen.logout_request:
        screen.logout_request = False
        from radiko.storage import SessionStore

        store = SessionStore(screen.data_dir) if screen.data_dir else SessionStore.default()
        store.delete()
        screen.auth = "signed-out"
        screen._set_notice(NoticeLevel.INFO, "ローカルのセッションを削除しました", False)
    if screen.login_request is not None:
        mail, password = screen.login_request
        screen.login_request = None
        screen.password = ""
        try:
            from radiko.auth import MemberClient
            from radiko.storage import SessionStore

            session, _membership = MemberClient.create().login(mail, password)
            store = SessionStore(screen.data_dir) if screen.data_dir else SessionStore.default()
            store.save(session)
            screen.auth = "signed-in"
            screen._set_notice(NoticeLevel.INFO, "ログインし、セッションを保存しました", False)
        except Exception as error:
            message = str(error).replace(password, "■") if password else str(error)
            screen._set_notice(NoticeLevel.ERROR, message, True)
        finally:
            password = ""
    if screen.fetch_chapters:
        screen.fetch_chapters = False
        rows = screen.visible()
        if not rows:
            screen._set_notice(NoticeLevel.WARNING, "番組を選択してください", True)
        else:
            program = rows[min(screen.selected, len(rows) - 1)]
            screen.chapter_program = program
            try:
                from radiko.prepare import inspect_remote_chapters

                view = inspect_remote_chapters(program.key.station_id, program.key.starts_at)
                screen.chapters = list(view.rows or [])
                screen.chapter_index = 0
                if not screen.chapters:
                    screen._set_notice(NoticeLevel.WARNING, view.issue or "チャプター未提供", True)
            except Exception as error:
                screen._set_notice(NoticeLevel.ERROR, str(error), True)
    if screen.play_request is None:
        return
    program, offset = screen.play_request
    screen.play_request = None
    try:
        from radiko.auth import MemberClient
        from radiko.prepare import play_station
        from radiko.storage import SessionStore

        store = SessionStore(screen.data_dir) if screen.data_dir else SessionStore.default()
        session = store.load()
        if session is None:
            screen._set_notice(
                NoticeLevel.ERROR, "未ログインです。loginでログインしてください", True
            )
            return
        membership = MemberClient.create().check(session)
        report = play_station(
            program.key.station_id,
            "live" if screen.live else "timefree",
            program.key.starts_at,
            screen.now,
            session,
            membership,
            offset,
            None,
            lambda: screen.quit,
        )
        screen.played_frames = report.frames
        screen._set_notice(NoticeLevel.INFO, f"再生フレーム {report.frames}", False)
    except Exception as error:
        screen._set_notice(NoticeLevel.ERROR, str(error), True)
    screen.revision += 1


def _fetch_programs(screen: Screen) -> None:
    from radiko.catalog import CatalogClient, CatalogError
    from radiko.http_client import catalog_fetch

    day = broadcast_day(screen.now)
    screen.area_failures = []
    screen.schedule_failures = []
    try:
        listed = CatalogClient(catalog_fetch).programs(screen.station_id, day)
    except CatalogError as error:
        screen.schedule_failures.append((screen.station_id, day.strftime("%Y%m%d"), str(error)))
        screen._set_notice(NoticeLevel.WARNING, str(error), True)
        return
    screen.programs = listed.programs
    screen.invalid_count = listed.invalid_count
    screen.selected = min(screen.selected, max(len(screen.visible()) - 1, 0))
    screen._set_notice(NoticeLevel.INFO, f"取得完了 {len(listed.programs)}枠", False)


def run(args: list[str], data_dir: Path | None) -> None:
    if args == ["--help"] or "--help" in args:
        from radiko_client.io import stdout_ln

        stdout_ln(USAGE)
        return
    offline, rest = split_args(args)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("対話端末で実行してください")
    station = next((arg for arg in rest if not arg.startswith("-")), "TBS")
    screen = Screen(
        [], datetime.now().astimezone(), offline=offline, data_dir=data_dir, station_id=station
    )
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
        painted = -1
        while not screen.quit:
            screen.poll_device()
            perform_pending(screen)
            if screen.revision != painted:
                sys.stdout.write("\x1b[H")
                for line in screen.render():
                    sys.stdout.write(line + "\x1b[K\n")
                sys.stdout.flush()
                painted = screen.revision
            key = _read_key(0.05)
            if key:
                screen.handle(key)
                screen.revision += 1
    finally:
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _read_byte(timeout: float | None) -> str | None:
    import os
    import select

    fd = sys.stdin.fileno()
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        return None
    try:
        raw = os.read(fd, 1)
    except BlockingIOError:
        return None
    if not raw:
        return None
    return raw.decode("utf-8", "ignore")


def _read_key(timeout: float | None = None) -> str | None:
    char = _read_byte(timeout)
    if not char:
        return None
    if char == "\x03":
        return "ctrl-c"
    if char == "\x1b":
        rest = _read_byte(0.02)
        if rest == "[":
            code = _read_byte(0.02) or ""
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
