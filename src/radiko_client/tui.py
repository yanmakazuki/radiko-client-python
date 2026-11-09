"""検索と再生の TUI。描画は状態を読むだけで、通信と復号は呼ばない。"""

from __future__ import annotations

import functools
import os
import queue
import select
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from pathlib import Path

import radiko.catalog as catalog
from radiko.auth import AuthError, MemberClient
from radiko.catalog import (
    acquire_range,
    acquire_stations,
    parse_day,
    parse_programs,
)
from radiko.domain import (
    STATUS_LABEL,
    BroadcastRange,
    BroadcastStatus,
    Filter,
    Program,
    Scope,
    SearchCriteria,
    broadcast_day,
    broadcast_status,
)
from radiko.http_client import catalog_fetch
from radiko.output import OutputError, diagnose_default_output
from radiko.prepare import inspect_remote_chapters, play_station
from radiko.storage import SearchStore, SessionStore, default_data_dir
from radiko.stream import validate_station
from radiko.text import cluster_width, display_width, graphemes
from radiko_client.io import stdout_ln

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
AREA_CHOICES = ["全国", *[f"JP{number}" for number in range(1, 48)]]


@dataclass
class FilterForm:
    """全条件。F2 は検索せず、Enter で条件を確定する。"""

    start: str
    end: str
    scope: str
    genres: str = ""
    personalities: str = ""
    keyword: str = ""
    unclassified_program: bool = False
    unclassified_personality: bool = False
    focus: int = 0
    error: str = ""
    picker: list[str] | None = None
    picker_index: int = 0
    picker_field: int | None = None

    def line(self) -> str:
        if self.picker is not None:
            current = self.picker[self.picker_index]
            return f"名称選択: {current}\n上下 選択 / Enter 条件へ反映（検索しません） / Esc 取消"
        labels = [
            f"開始: {self.start}",
            f"終了: {self.end or '（開始と同じ）'}",
            f"範囲: {self.scope}",
            f"番組ジャンル: {self.genres or '指定なし'}",
            f"出演者分類: {self.personalities or '指定なし'}",
            f"キーワード: {self.keyword or '指定なし'}",
            f"番組未分類: {'オン' if self.unclassified_program else 'オフ'}",
            f"出演者未分類: {'オン' if self.unclassified_personality else 'オフ'}",
            "観測した分類",
        ]
        body = "\n".join(
            f"{'>' if index == self.focus else ' '} {label}" for index, label in enumerate(labels)
        )
        hint = "Tab・上下 項目 / F2 地域・分類を名称選択 / Space 未分類 / Enter 検索 / Esc 取消"
        return f"{body}\n{self.error}\n{hint}"

    def criteria(self) -> SearchCriteria:
        start = parse_day(self.start)
        end = start if not self.end.strip() else parse_day(self.end.strip())
        scope_text = self.scope.strip()
        if scope_text in {"全国", "all", "ALL"}:
            scope = Scope.nationwide()
        elif scope_text.startswith("局:") or scope_text.lower().startswith("station:"):
            station = scope_text.split(":", 1)[1].strip()
            scope = Scope.station(station)
        else:
            areas = [item.strip() for item in scope_text.split(",") if item.strip()]
            scope = Scope.areas(areas)
        keyword = self.keyword.strip() or None
        genres = [item.strip() for item in self.genres.split(",") if item.strip()]
        personalities = [item.strip() for item in self.personalities.split(",") if item.strip()]
        return SearchCriteria.create(
            scope,
            BroadcastRange.new(start, end),
            Filter.create(
                genres,
                personalities,
                self.unclassified_program,
                self.unclassified_personality,
                keyword,
            ),
        )


@functools.lru_cache(maxsize=1)
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
        points = _boundaries(self.value)
        self.cursor = _snap_points(points, self.cursor)
        raw = self.value.encode()
        index = points.index(self.cursor)
        if key == "ctrl-u":
            self.value = ""
            self.cursor = 0
        elif key == "backspace":
            start = points[index - 1] if index > 0 else 0
            self.value = (raw[:start] + raw[self.cursor :]).decode()
            self.cursor = start
        elif key == "delete":
            end = points[index + 1] if index + 1 < len(points) else len(raw)
            self.value = (raw[: self.cursor] + raw[end:]).decode()
        elif key == "left":
            self.cursor = points[index - 1] if index > 0 else 0
        elif key == "right":
            self.cursor = points[index + 1] if index + 1 < len(points) else len(raw)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(raw)
        elif (
            len(key) == 1
            and key.isprintable()
            and not key.isspace()
            or (len(key) == 1 and key not in {"\n", "\t"} and key.isprintable())
        ):
            self._insert(key)
        # 結合文字の挿入などでクラスタ内部を指した場合も、旧実装と同じ境界へ戻す。
        self.cursor = _snap_points(
            _boundaries(self.value), min(self.cursor, len(self.value.encode()))
        )

    def _insert(self, char: str) -> None:
        if len(self.value.encode()) + len(char.encode()) > self.byte_limit:
            return
        if self.scalar_limit is not None and len(self.value) >= self.scalar_limit:
            return
        raw = self.value.encode()
        self.value = (raw[: self.cursor] + char.encode() + raw[self.cursor :]).decode()
        self.cursor += len(char.encode())

    def view(self, columns: int) -> tuple[str, int]:
        if columns <= 0:
            return self.value, 0
        clusters = graphemes(self.value)
        widths = [cluster_width(cluster) for cluster in clusters]
        prefix = [0] * (len(widths) + 1)
        for position, width in enumerate(widths):
            prefix[position + 1] = prefix[position] + width
        index = 0
        cursor_cluster = len(clusters)
        for position, cluster in enumerate(clusters):
            if index >= self.cursor:
                cursor_cluster = position
                break
            index += len(cluster.encode())
        start = 0
        while start < cursor_cluster and prefix[cursor_cluster] - prefix[start] >= columns:
            start += 1
        end = cursor_cluster
        while end < len(clusters) and prefix[end + 1] - prefix[start] <= columns:
            end += 1
        text = "".join(clusters[start:end])
        column = prefix[cursor_cluster] - prefix[start]
        if column >= columns:
            column = max(columns - 1, 0)
        return text, column


def _boundaries(value: str) -> list[int]:
    points = [0]
    for cluster in graphemes(value):
        points.append(points[-1] + len(cluster.encode()))
    return points


def _snap_points(points: list[int], cursor: int) -> int:
    return min(points, key=lambda point: (abs(point - cursor), point))


def _snap(value: str, cursor: int) -> int:
    points = _boundaries(value)
    return _snap_points(points, cursor)


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
    current: list[str] = []
    used = 0
    for cluster in graphemes(text):
        size = cluster_width(cluster)
        if current and used + size > width:
            lines.append("".join(current))
            current = []
            used = 0
        current.append(cluster)
        used += size
    lines.append("".join(current))
    return lines


def _clock(seconds: float) -> str:
    total = max(int(seconds), 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def truncate_width(text: str, width: int) -> str:
    if width <= 0:
        return ""
    kept: list[str] = []
    used = 0
    for cluster in graphemes(text):
        used += cluster_width(cluster)
        if used > width:
            return "".join(kept)
        kept.append(cluster)
    return text


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
    seconds_error: str = ""
    play_mode: str = "timefree"
    auth_refresh: bool = False
    data_dir: Path | None = None
    criteria: SearchCriteria | None = None
    chapter_summaries: list[str] = field(default_factory=list)
    chapter_summary_focused: bool = False
    chapter_scroll: int = 0
    filter_form: FilterForm | None = None
    job_generation: int = 0
    job_cancel: object | None = None
    job_box: object | None = None
    job_thread: object | None = None

    def _quit(self) -> None:
        self.quit = True
        cancel = self.job_cancel
        if cancel is not None:
            cancel.set()

    def ensure_criteria(self) -> SearchCriteria:
        if self.criteria is None:
            self.criteria = SearchCriteria.initial(self.now)
        return self.criteria

    def visible(self) -> list[Program]:
        return [program for program in self.programs if self.committed.matches(program)]

    def handle(self, key: str) -> None:
        self.clock += 0.05
        self._expire_info()
        if key == "ctrl-c":
            self._quit()
            return
        if self.area == "login":
            self._login_key(key)
            return
        if self.area == "chapters":
            self._chapters(key)
            return
        if self.area == "filter":
            self._filter_key(key)
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
            self._quit()
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
            self._open_filter()
        elif key == "b":
            self._open_named()
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
            self._open_seconds()
        elif key == "l":
            self._live()
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
                self._set_notice(
                    NoticeLevel.INFO, "オフライン確認ではセッションを通信確認しません", sticky=False
                )
            else:
                self.auth_refresh = True
        elif key == "enter":
            self._enter()
        elif key == "r":
            self._commit_search()

    def keyword_preview(self, rows: list[Program] | None = None) -> str | None:
        if self.area != "keyword":
            return None
        try:
            draft = self.committed.with_keyword(self.draft_keyword.value or None)
        except ValueError:
            draft = self.committed
        visible = self.visible() if rows is None else rows
        count = sum(draft.matches(program) for program in visible)
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
        if self.area == "seconds":
            return self._framed("位置指定", self._seconds_body())
        if self.area == "filter" and self.filter_form is not None:
            return self._framed("全条件", self.filter_form.line())
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
            else self.keyword_preview(rows)
            or f"番組一覧 一致{len(rows)} / 取得{len(self.programs)}枠"
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

    def _scope(self, rows: list[Program]) -> str:
        if not rows:
            return "指定なし"
        return rows[0].key.station_id

    def _mode_label(self) -> str:
        mode = "ライブ（放送中のみ）" if self.live else "タイムフリー"
        if self.offline:
            return f"{mode} / オフライン確認"
        return mode

    def _header(self, compact: bool, rows: list[Program]) -> list[str]:
        scope = self._scope(rows)
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
            notice = self.notice.text if self.notice else ""
            return "チャプター未提供" + (f"\n{notice}" if notice else "")
        lines = []
        for index, (begin, finish, title) in enumerate(self.chapters):
            mark = ">" if index == self.chapter_index else " "
            lines.append(f"{mark} {begin:.3f}–{finish:.3f}秒 {title}")
        summary = ""
        if self.chapter_summaries and self.chapter_index < len(self.chapter_summaries):
            summary = self.chapter_summaries[self.chapter_index]
        shown = summary[self.chapter_scroll : self.chapter_scroll + 1200]
        if self.chapter_summary_focused:
            lines.append("要約（先頭1200文字まで）・上下/ページ スクロール・Tab 章一覧")
        else:
            lines.append("選択したチャプターの要約（Tabで拡大・スクロール）")
        lines.append(shown)
        return "\n".join(lines)

    def _open_filter(self) -> None:
        criteria = self.ensure_criteria()
        scope = criteria.scope
        if scope.kind == "nationwide":
            scope_text = "全国"
        elif scope.kind == "station":
            scope_text = f"局:{scope.value}"
        else:
            scope_text = ",".join(scope.value or [])
        end = ""
        if criteria.range.start != criteria.range.end:
            end = criteria.range.end.strftime("%Y%m%d")
        self.filter_form = FilterForm(
            criteria.range.start.strftime("%Y%m%d"),
            end,
            scope_text,
            ",".join(criteria.filter.program_genres),
            ",".join(criteria.filter.personality_genres),
            criteria.filter.keyword or "",
            criteria.filter.unclassified_program,
            criteria.filter.unclassified_personality,
        )
        self.area = "filter"

    def _filter_key(self, key: str) -> None:
        form = self.filter_form
        if form is None:
            self.area = "list"
            return
        if form.picker is not None:
            if key in {"esc", "q"}:
                form.picker = None
                return
            if key in {"j", "down"}:
                form.picker_index = min(form.picker_index + 1, len(form.picker) - 1)
                return
            if key in {"k", "up"}:
                form.picker_index = max(form.picker_index - 1, 0)
                return
            if key == "enter":
                chosen = form.picker[form.picker_index]
                if form.picker_field == 2:
                    form.scope = chosen
                elif form.picker_field == 3:
                    form.genres = chosen.split(" ", 1)[0]
                elif form.picker_field == 4:
                    form.personalities = chosen.split(" ", 1)[0]
                form.picker = None
            return
        if key == "esc":
            self.area = "list"
            self.filter_form = None
            return
        if key in {"tab", "down"}:
            form.focus = (form.focus + 1) % 9
            return
        if key == "up":
            form.focus = (form.focus + 8) % 9
            return
        if key == "f2":
            if form.focus == 2:
                form.picker = list(AREA_CHOICES)
                form.picker_index = 0
                form.picker_field = 2
            elif form.focus in {3, 4}:
                form.picker = ["UNKNOWN 未知分類", "C999 出演分類"]
                form.picker_index = 0
                form.picker_field = form.focus
            return
        if key == " " and form.focus == 6:
            form.unclassified_program = not form.unclassified_program
            return
        if key == " " and form.focus == 7:
            form.unclassified_personality = not form.unclassified_personality
            return
        if key == "enter":
            try:
                self.criteria = form.criteria()
            except (ValueError, Exception) as error:
                form.error = str(error)
                return
            self.committed = self.criteria.filter
            self.filter_form = None
            self._commit_search()
            return
        target = {
            0: "start",
            1: "end",
            2: "scope",
            3: "genres",
            4: "personalities",
            5: "keyword",
        }.get(form.focus)
        if target is None:
            return
        current = getattr(form, target)
        if key == "ctrl-u":
            setattr(form, target, "")
        elif key == "backspace":
            setattr(form, target, current[:-1])
        elif len(key) == 1 and key.isprintable():
            setattr(form, target, current + key)

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
        if key == "tab":
            self.chapter_summary_focused = not self.chapter_summary_focused
            return
        if key in {"q", "esc"}:
            self.area = "list"
            self.chapter_summary_focused = False
            return
        if self.chapter_summary_focused and key in {"j", "down"}:
            self.chapter_scroll += 1
            return
        if self.chapter_summary_focused and key in {"k", "up"}:
            self.chapter_scroll = max(0, self.chapter_scroll - 1)
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
        self.device_generation += 1
        generation = self.device_generation
        self.device_status = "loading"
        self.device_text = "取得中…"
        box: queue.Queue = queue.Queue(1)
        self.device_box = box
        probe = self.device_probe

        def work() -> None:
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

    def _selected_program(self) -> Program | None:
        rows = self.visible()
        if not rows:
            return None
        return rows[min(self.selected, len(rows) - 1)]

    def _start(self, program: Program, mode: str, offset: float = 0.0) -> None:
        status = broadcast_status(program.key.starts_at, program.ends_at, self.now)
        if mode == "live" and status != BroadcastStatus.ON_AIR:
            text = (
                "放送前のため再生できません（現在の再生を継続）"
                if status == BroadcastStatus.UPCOMING
                else "放送終了のためライブ再生できません（現在の再生を継続）"
            )
            self._set_notice(NoticeLevel.WARNING, text, sticky=True)
            return
        if mode == "timefree" and status == BroadcastStatus.UPCOMING:
            self._set_notice(
                NoticeLevel.WARNING, "放送前のため再生できません（現在の再生を継続）", sticky=True
            )
            return
        if self.offline:
            self._set_notice(NoticeLevel.INFO, "オフライン確認では再生しません", sticky=False)
            return
        self.playing = program
        self.position = offset
        self.play_mode = mode
        self.paused = False
        self.play_request = (program, offset)
        self._set_notice(NoticeLevel.INFO, "再生準備中", sticky=False)

    def _enter(self) -> None:
        if self.area == "filter":
            self._commit_search()
            return
        program = self._selected_program()
        if program is None:
            self._set_notice(NoticeLevel.INFO, "移動できる番組がありません", sticky=False)
            return
        self._start(program, "live" if self.live else "timefree")

    def _live(self) -> None:
        if self.offline:
            self._set_notice(NoticeLevel.INFO, "オフライン確認では再生しません", sticky=False)
            return
        program = self._selected_program()
        if program is None:
            return
        self._start(program, "live")

    def _open_seconds(self) -> None:
        if self.playing is None:
            self._set_notice(NoticeLevel.INFO, "再生中の番組はありません", sticky=False)
            return
        if self.play_mode == "live":
            self._set_notice(
                NoticeLevel.WARNING, "ライブは位置指定できません（現在の再生を継続）", sticky=True
            )
            return
        self.seconds_text = ""
        self.seconds_error = ""
        self.area = "seconds"

    def _seconds_body(self) -> str:
        if self.playing is None:
            return "再生中の番組はありません"
        total = int((self.playing.ends_at - self.playing.key.starts_at).total_seconds())
        return (
            f"位置指定: {self.playing.key.station_id} / 枠内の秒数（0以上{total}未満）: "
            f"{self.seconds_text}\nEnter 確定 / Esc 取消 / Ctrl+U 消去 / {self.seconds_error}"
        )

    def _open_named(self) -> None:
        if self.offline:
            self._set_notice(
                NoticeLevel.INFO, "オフライン確認では保存条件を読み書きしません", sticky=False
            )
            return

        store = SearchStore(self.data_dir or default_data_dir())
        try:
            library = store.load()
        except Exception as error:
            self._set_notice(NoticeLevel.ERROR, f"保存条件の操作失敗: {error}", True)
            return
        self.named = dict(library.named)
        self.area = "named"

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
                self.criteria = criteria
                self.committed = criteria.filter
                if criteria.scope.kind == "station" and isinstance(criteria.scope.value, str):
                    self.station_id = criteria.scope.value
                self._set_notice(
                    NoticeLevel.INFO,
                    f"「{name}」を呼び出しました "
                    f"{criteria.range.start.strftime('%Y%m%d')}"
                    f"〜{criteria.range.end.strftime('%Y%m%d')}",
                    sticky=False,
                )
            else:
                self._set_notice(
                    NoticeLevel.WARNING, f"「{name}」は保存されていません", sticky=True
                )
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
        if key == "ctrl-u":
            self.seconds_text = ""
            self.seconds_error = ""
            return
        if key == "backspace":
            self.seconds_text = self.seconds_text[:-1]
            self.seconds_error = ""
            return
        if key == "enter":
            if self.playing is None:
                self.area = "list"
                return
            total = (self.playing.ends_at - self.playing.key.starts_at).total_seconds()
            try:
                value = int(self.seconds_text)
            except ValueError:
                value = -1
            if not 0 <= value < total:
                self.seconds_error = "放送枠内の整数秒を入力してください"
                return
            self.position = float(value)
            self.play_mode = "timefree"
            self.play_request = (self.playing, float(value))
            self.area = "list"
            self._set_notice(NoticeLevel.INFO, f"{value}秒から再生します", sticky=False)
            return
        if key.isdigit() and len(self.seconds_text) < 20:
            self.seconds_text += key
            self.seconds_error = ""

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
    if screen.auth_refresh:
        screen.auth_refresh = False
        _refresh_auth(screen)
    if screen.fetch_programs:
        screen.fetch_programs = False
        _start_job(screen, "fetch", screen.ensure_criteria())
    if screen.logout_request:
        screen.logout_request = False

        store = SessionStore(screen.data_dir) if screen.data_dir else SessionStore.default()
        store.delete()
        screen.auth = "signed-out"
        screen._set_notice(NoticeLevel.INFO, "ローカルのセッションを削除しました", False)
    if screen.login_request is not None:
        mail, password = screen.login_request
        screen.login_request = None
        screen.password = ""
        try:
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
            _start_job(screen, "chapters", program)
    if screen.play_request is not None:
        program, offset = screen.play_request
        screen.play_request = None
        _start_job(screen, "play", (program, offset, screen.play_mode, screen.now, screen.data_dir))


def _refresh_auth(screen: Screen) -> None:
    store = SessionStore(screen.data_dir) if screen.data_dir else SessionStore.default()
    try:
        session = store.load()
    except Exception as error:
        screen.auth = "signed-out"
        screen._set_notice(NoticeLevel.ERROR, str(error), True)
        return
    if session is None:
        screen.auth = "signed-out"
        screen._set_notice(NoticeLevel.INFO, "未ログインです。loginでログインしてください", False)
        return
    try:
        membership = MemberClient.create().check(session)
    except AuthError as error:
        screen.auth = "signed-out"
        screen._set_notice(NoticeLevel.ERROR, str(error), True)
        return
    screen.auth = "signed-in"

    def label(value: bool | None) -> str:
        if value is True:
            return "あり"
        if value is False:
            return "なし"
        return "未確認"

    screen._set_notice(
        NoticeLevel.INFO,
        f"ログイン有効 / エリアフリー: {label(membership.area_free)} / "
        f"タイムフリー30: {label(membership.time_free_30)}",
        False,
    )


def _fetch_programs(criteria: SearchCriteria, cancel) -> dict:
    client = catalog.CatalogClient(catalog_fetch)
    scope = criteria.scope
    if scope.kind == "station" and isinstance(scope.value, str):
        report = acquire_stations(
            client, [scope.value], criteria.range, cancel, lambda _progress: None
        )
    else:
        if scope.kind == "areas":
            areas = list(scope.value or [])
        else:
            areas = [f"JP{number}" for number in range(1, 48)]
        report = acquire_range(client, areas, criteria.range, cancel, lambda _progress: None)
    programs = [
        program
        for listing in report.lists
        for program in listing.programs
        if criteria.filter.matches(program)
    ]
    invalid = sum(listing.invalid_count for listing in report.lists)
    area_failures = [(failure.target, str(failure.error)) for failure in report.area_failures]
    schedule_failures = [
        (
            failure.target,
            failure.day.strftime("%Y%m%d") if failure.day else "",
            str(failure.error),
        )
        for failure in report.schedule_failures
    ]
    return {
        "programs": programs,
        "invalid": invalid,
        "area_failures": area_failures,
        "schedule_failures": schedule_failures,
    }


def _start_job(screen: Screen, kind: str, payload) -> None:
    previous = screen.job_cancel
    if previous is not None:
        previous.set()
    screen.job_generation += 1
    generation = screen.job_generation
    cancel = threading.Event()
    box: queue.Queue = queue.Queue(1)
    screen.job_cancel = cancel
    screen.job_box = box

    def work() -> None:
        try:
            if kind == "fetch":
                result = ("fetch", _fetch_programs(payload, cancel))
            elif kind == "chapters":
                program = payload
                view = inspect_remote_chapters(
                    program.key.station_id, program.key.starts_at, cancel.is_set
                )
                result = ("chapters", (program, view))
            else:
                program, offset, mode, now, data_dir = payload

                store = SessionStore(data_dir) if data_dir else SessionStore.default()
                session = store.load()
                if session is None:
                    raise RuntimeError("未ログインです。loginでログインしてください")
                membership = MemberClient.create().check(session)
                report = play_station(
                    program.key.station_id,
                    mode,
                    program.key.starts_at,
                    now,
                    session,
                    membership,
                    offset,
                    None,
                    cancel.is_set,
                )
                result = ("play", report)
            box.put((generation, result, None))
        except Exception as error:
            box.put((generation, None, error))

    thread = threading.Thread(target=work, daemon=True)
    screen.job_thread = thread
    thread.start()


def poll_jobs(screen: Screen) -> None:
    box = screen.job_box
    if box is None:
        return
    try:
        generation, result, error = box.get_nowait()
    except queue.Empty:
        return
    if generation != screen.job_generation:
        return
    screen.job_box = None
    screen.revision += 1
    if error is not None:
        if getattr(error, "code", None) == "cancelled":
            return
        screen._set_notice(NoticeLevel.ERROR, str(error), True)
        return
    kind, value = result
    if kind == "fetch":
        screen.programs = value["programs"]
        screen.invalid_count = value["invalid"]
        screen.area_failures = value["area_failures"]
        screen.schedule_failures = value["schedule_failures"]
        screen.selected = min(screen.selected, max(len(screen.visible()) - 1, 0))
        if screen.schedule_failures or screen.area_failures:
            screen._set_notice(NoticeLevel.WARNING, "一部の番組表を取得できませんでした", True)
        else:
            screen._set_notice(NoticeLevel.INFO, f"取得完了 {len(screen.programs)}枠", False)
    elif kind == "chapters":
        program, view = value
        screen.chapter_program = program
        screen.chapters = list(view.rows or [])
        screen.chapter_summaries = list(view.summaries or [])
        screen.chapter_index = 0
        screen.chapter_scroll = 0
        screen.area = "chapters"
        if not screen.chapters:
            screen._set_notice(NoticeLevel.WARNING, view.issue or "チャプター未提供", True)
    elif kind == "play":
        screen.played_frames = value.frames
        if value.reason == "cancelled":
            screen._set_notice(NoticeLevel.INFO, "中断しました", False)
        else:
            screen._set_notice(NoticeLevel.INFO, f"再生フレーム {value.frames}", False)


def wait_jobs(screen: Screen, timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        poll_jobs(screen)
        thread = screen.job_thread
        if thread is None or not thread.is_alive():
            poll_jobs(screen)
            return
        time.sleep(0.02)
    poll_jobs(screen)


def run(args: list[str], data_dir: Path | None) -> None:
    offline, rest = split_args(args)
    if rest == ["--help"]:
        stdout_ln(USAGE)
        return
    if any(arg in {"--json", "--classifications"} for arg in rest):
        raise RuntimeError("TUIでは表示用CLIオプションは使用できません")
    now = datetime.now().astimezone()
    if rest:
        from radiko_client.cli import parse_program_request

        parse_program_request(rest, now)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("tuiは対話端末で実行してください")
    positional = [arg for arg in rest if not arg.startswith("-")]
    station = positional[0] if positional else "TBS"
    screen = Screen([], now, offline=offline, data_dir=data_dir, station_id=station)
    if positional:
        day = parse_day(positional[1]) if len(positional) > 1 else broadcast_day(screen.now)
        screen.criteria = SearchCriteria.create(
            Scope.station(positional[0]),
            BroadcastRange.single(day),
            Filter.create(),
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
    before = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_args: setattr(screen, "quit", True))
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?1049h\x1b[2J")
        sys.stdout.flush()
        service_loop(screen, _read_key, _paint)
    finally:
        signal.signal(signal.SIGINT, before)
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _paint(screen: Screen) -> None:
    sys.stdout.write("\x1b[H")
    for line in screen.render():
        sys.stdout.write(line + "\x1b[K\n")
    sys.stdout.flush()


def service_loop(screen: Screen, read_key, paint=None) -> None:
    """キーを読みながら、取得と再生は別スレッドの世代だけを反映する。"""
    painted = -1
    while not screen.quit:
        screen.poll_device()
        perform_pending(screen)
        poll_jobs(screen)
        if paint is not None and screen.revision != painted:
            paint(screen)
            painted = screen.revision
        key = read_key(0.05)
        if key:
            screen.handle(key)
            screen.revision += 1


def _read_raw(timeout: float | None) -> bytes | None:
    fd = sys.stdin.fileno()
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        return None
    try:
        raw = os.read(fd, 1)
    except BlockingIOError:
        return None
    return raw or None


def _read_key(timeout: float | None = None) -> str | None:
    raw = _read_raw(timeout)
    if not raw:
        return None
    if raw[0] >= 0x80:
        extra = 1 if raw[0] < 0xE0 else 2 if raw[0] < 0xF0 else 3
        parts = bytearray(raw)
        while len(parts) < 1 + extra:
            more = _read_raw(0.02)
            if not more:
                break
            parts.extend(more)
        char = bytes(parts).decode("utf-8", "ignore")
    else:
        char = raw.decode("ascii", "ignore")
    if not char:
        return None
    if char == "\x03":
        return "ctrl-c"
    if char == "\x15":
        return "ctrl-u"
    if char == "\t":
        return "tab"
    if char == "\x1b":
        rest = _read_raw(0.02)
        if rest == b"[":
            code = ""
            while True:
                nxt = _read_raw(0.02)
                if not nxt:
                    break
                code += nxt.decode("ascii", "ignore")
                if code.endswith("~") or code[-1:].isalpha():
                    break
            if code == "12~":
                return "f2"
            return {
                "A": "up",
                "B": "down",
                "C": "right",
                "D": "left",
                "H": "home",
                "F": "end",
            }.get(code, "esc")
        if rest == b"O" and _read_raw(0.02) == b"Q":
            return "f2"
        return "esc"
    if char in {"\r", "\n"}:
        return "enter"
    if char == "\x7f":
        return "backspace"
    if char == " ":
        return " "
    return char
