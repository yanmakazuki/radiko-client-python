"""放送枠、放送日、検索条件。通信と端末には依存しない。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from radiko.text import has_control

JST = timezone(timedelta(hours=9))
VALID_AREAS = frozenset(f"JP{number}" for number in range(1, 48))


def parse_jst(text: str) -> datetime:
    """`YYYY-MM-DDTHH:MM:SS+09:00` または末尾 Z。"""
    if text.endswith("Z"):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("タイムゾーンがありません")
    return parsed.astimezone(JST)


def rfc3339(moment: datetime) -> str:
    """端末のタイムゾーンではなく、値が持つ UTC オフセットを保つ。"""
    stamp = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )
    fraction = ""
    if moment.microsecond:
        digits = f"{moment.microsecond:06d}"
        fraction = "." + (digits[:3] if moment.microsecond % 1000 == 0 else digits)
    offset = moment.utcoffset()
    seconds = 0 if offset is None else int(offset.total_seconds())
    sign = "-" if seconds < 0 else "+"
    seconds = abs(seconds)
    return f"{stamp}{fraction}{sign}{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def broadcast_day(now: datetime) -> date:
    """入力のタイムゾーンによらず、日本時間の午前5時を放送日の境界にする。"""
    local = now.astimezone(JST)
    day = local.date()
    if local.hour < 5:
        return day - timedelta(days=1)
    return day


class BroadcastStatus:
    """放送前・放送中・放送終了の3値。

    `broadcast_status` が now と開始・終了時刻を比べて決める。放送日は JST 05:00 で切り替わる。
    """

    UPCOMING = "upcoming"
    ON_AIR = "on_air"
    FINISHED = "finished"


STATUS_LABEL = {
    BroadcastStatus.UPCOMING: "放送前",
    BroadcastStatus.ON_AIR: "放送中",
    BroadcastStatus.FINISHED: "放送終了",
}


def broadcast_status(start: datetime, end: datetime, now: datetime) -> str:
    if end <= start:
        raise ValueError("放送終了日時が開始日時以前です")
    if now < start:
        return BroadcastStatus.UPCOMING
    if now < end:
        return BroadcastStatus.ON_AIR
    return BroadcastStatus.FINISHED


class ListeningAvailability:
    """再生可否の観測値。実際に再生を試した結果だけを表す。

    CURRENT は「現在の再生で確認済み」で、局や条件を変えた再操作では再確認する。
    """

    UNKNOWN = "再生可否未確認"
    CURRENT = "現在の再生で配信確認済み（再操作時は再確認）"


@dataclass(frozen=True)
class ProgramKey:
    """局IDと開始時刻で番組を一意にするキー。番組表・保存・TUI の選択が共有する。"""

    station_id: str
    starts_at: datetime

    def __hash__(self) -> int:
        return hash((self.station_id, self.starts_at))


@dataclass(frozen=True)
class Classification:
    """番組や出演者の分類。radiko の genre id と表示名の組。"""

    id: str
    name: str


@dataclass
class Program:
    """番組表の1番組。catalog が作り、CLI と TUI が読む。"""

    key: ProgramKey
    program_id: str
    ends_at: datetime
    title: str
    performer: str = ""
    description: str = ""
    info: str = ""
    tags: list[str] = field(default_factory=list)
    program_genres: list[Classification] = field(default_factory=list)
    personality_genres: list[Classification] = field(default_factory=list)
    _search_text: str | None = field(default=None, repr=False, compare=False, init=False)

    @property
    def search_text(self) -> str:
        """検索用に小文字化した本文。初回だけ生成して再利用する。"""
        if self._search_text is None:
            self._search_text = "\n".join(
                (self.title, self.performer, self.description, self.info, *self.tags)
            ).lower()
        return self._search_text


@dataclass(frozen=True)
class Station:
    """放送局。保存と検索条件は局IDだけを使い、名前は表示用。"""

    id: str
    name: str


@dataclass
class BroadcastRange:
    """検索対象の放送日範囲。開始・終了と、列挙済みの days を持つ。"""

    start: date
    end: date
    days: list[date]

    @classmethod
    def single(cls, day: date) -> BroadcastRange:
        return cls(day, day, [day])

    @classmethod
    def new(cls, start: date, end: date) -> BroadcastRange:
        span = (end - start).days
        if not 0 <= span < 30:
            raise ValueError("期間は開始日から終了日までの1〜30放送日で指定してください")
        days = []
        day = start
        while True:
            days.append(day)
            if day >= end:
                break
            day = day + timedelta(days=1)
        return cls(start, end, days)


@dataclass
class Chapter:
    """チャプター1件。開始・終了は番組先頭からの秒で、source_index は元の並び。"""

    title: str
    summary: str
    start: float
    end: float
    source_index: int


@dataclass
class ChapterSet:
    """チャプターの集合。不正件数と音楽件数を添えて画面へ渡す。"""

    chapters: list[Chapter]
    invalid_count: int
    music_count: int

    def current(self, position: float) -> Chapter | None:
        """重複時は開始が最も後の項目。同じ開始なら元の順序を優先する。"""
        chosen: Chapter | None = None
        for chapter in self.chapters:
            if chapter.start <= position < chapter.end and (
                chosen is None
                or chapter.start > chosen.start
                or (chapter.start == chosen.start and chapter.source_index < chosen.source_index)
            ):
                chosen = chapter
        return chosen


def _ids(values: list[str]) -> list[str]:
    if any(item.strip() == "" or len(item.encode()) > 128 or has_control(item) for item in values):
        raise ValueError("分類IDは空でない128バイト以内の値で指定してください")
    return sorted({item.strip() for item in values})


@dataclass
class Filter:
    """検索条件。番組・出演者の分類ID、未分類の可否、キーワードを持つ。

    `matches` は指定した軸のすべてを AND で評価する。
    """

    program_genres: list[str] = field(default_factory=list)
    personality_genres: list[str] = field(default_factory=list)
    unclassified_program: bool = False
    unclassified_personality: bool = False
    keyword: str | None = None
    keyword_lowercase: str | None = field(default=None, repr=False, compare=False)
    program_genre_set: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)
    personality_genre_set: frozenset[str] = field(
        default_factory=frozenset, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.program_genre_set = frozenset(self.program_genres)
        self.personality_genre_set = frozenset(self.personality_genres)
        if self.keyword is not None and self.keyword_lowercase is None:
            self.keyword_lowercase = self.keyword.lower()

    @classmethod
    def create(
        cls,
        program_genres: list[str] | None = None,
        personality_genres: list[str] | None = None,
        unclassified_program: bool = False,
        unclassified_personality: bool = False,
        keyword: str | None = None,
    ) -> Filter:
        keyword_text = None if keyword is None else keyword.strip()
        if keyword_text is not None and (
            keyword_text == "" or len(keyword_text.encode()) > 1024 or has_control(keyword_text)
        ):
            raise ValueError("キーワードは空でない1024バイト以内の文字列で指定してください")
        result = cls(
            program_genres=_ids(list(program_genres or [])),
            personality_genres=_ids(list(personality_genres or [])),
            unclassified_program=unclassified_program,
            unclassified_personality=unclassified_personality,
            keyword=keyword_text,
            keyword_lowercase=None if keyword_text is None else keyword_text.lower(),
        )
        return result

    def with_keyword(self, keyword: str | None) -> Filter:
        return Filter.create(
            self.program_genres,
            self.personality_genres,
            self.unclassified_program,
            self.unclassified_personality,
            keyword,
        )

    def matches(self, program: Program) -> bool:
        return (
            _matches_class(
                program.program_genres, self.program_genre_set, self.unclassified_program
            )
            and _matches_class(
                program.personality_genres,
                self.personality_genre_set,
                self.unclassified_personality,
            )
            and (self.keyword_lowercase is None or self.keyword_lowercase in program.search_text)
        )

    def to_dict(self) -> dict:
        return {
            "program_genres": list(self.program_genres),
            "personality_genres": list(self.personality_genres),
            "unclassified_program": self.unclassified_program,
            "unclassified_personality": self.unclassified_personality,
            "keyword": self.keyword,
        }

    @classmethod
    def from_dict(cls, value: dict) -> Filter:
        _reject_unknown(
            value,
            {
                "program_genres",
                "personality_genres",
                "unclassified_program",
                "unclassified_personality",
                "keyword",
            },
        )
        genres = value["program_genres"]
        personalities = value["personality_genres"]
        unclassified_program = value["unclassified_program"]
        unclassified_personality = value["unclassified_personality"]
        keyword = value["keyword"]
        if (
            not isinstance(genres, list)
            or not isinstance(personalities, list)
            or not isinstance(unclassified_program, bool)
            or not isinstance(unclassified_personality, bool)
            or not (keyword is None or isinstance(keyword, str))
            or not all(isinstance(item, str) for item in genres)
            or not all(isinstance(item, str) for item in personalities)
        ):
            raise ValueError("invalid")
        return cls.create(
            genres,
            personalities,
            unclassified_program,
            unclassified_personality,
            keyword,
        )


def _matches_class(items: list[Classification], ids: frozenset[str], empty: bool) -> bool:
    if not ids and not empty:
        return True
    if empty and not items:
        return True
    return any(item.id in ids for item in items)


@dataclass(frozen=True)
class ObservedClassifications:
    """取得した番組から観測した分類と、分類なしの件数。放送局の選択肢に使う。"""

    program_genres: list[Classification]
    personality_genres: list[Classification]
    unclassified_programs: int
    unclassified_personalities: int

    @classmethod
    def collect(cls, programs: list[Program]) -> ObservedClassifications:
        genres: set[tuple[str, str]] = set()
        personalities: set[tuple[str, str]] = set()
        unclassified_programs = 0
        unclassified_personalities = 0
        for program in programs:
            unclassified_programs += int(not program.program_genres)
            unclassified_personalities += int(not program.personality_genres)
            for item in program.program_genres:
                genres.add((item.id, item.name))
            for item in program.personality_genres:
                personalities.add((item.id, item.name))
        return cls(
            [Classification(item_id, name) for item_id, name in sorted(genres)],
            [Classification(item_id, name) for item_id, name in sorted(personalities)],
            unclassified_programs,
            unclassified_personalities,
        )

    @classmethod
    def merge(cls, observed: list[ObservedClassifications]) -> ObservedClassifications:
        """`collect` の結果を、番組を走査し直さずに統合する。"""
        genres: set[tuple[str, str]] = set()
        personalities: set[tuple[str, str]] = set()
        unclassified_programs = 0
        unclassified_personalities = 0
        for item in observed:
            genres.update((entry.id, entry.name) for entry in item.program_genres)
            personalities.update((entry.id, entry.name) for entry in item.personality_genres)
            unclassified_programs += item.unclassified_programs
            unclassified_personalities += item.unclassified_personalities
        return cls(
            [Classification(item_id, name) for item_id, name in sorted(genres)],
            [Classification(item_id, name) for item_id, name in sorted(personalities)],
            unclassified_programs,
            unclassified_personalities,
        )


class ScopeKind:
    """検索範囲の種類。全国・1局・複数エリアの3つ。"""

    NATIONWIDE = "nationwide"
    STATION = "station"
    AREAS = "areas"


@dataclass
class Scope:
    """検索範囲。全国は value なし、局は局ID、エリアは地域IDの並びを持つ。"""

    kind: str
    value: str | list[str] | None = None

    @classmethod
    def nationwide(cls) -> Scope:
        return cls(ScopeKind.NATIONWIDE)

    @classmethod
    def station(cls, station_id: str) -> Scope:
        return cls(ScopeKind.STATION, station_id)

    @classmethod
    def areas(cls, areas: list[str]) -> Scope:
        return cls(ScopeKind.AREAS, list(areas))

    def to_dict(self) -> dict:
        if self.kind == ScopeKind.NATIONWIDE:
            return {"kind": "nationwide"}
        return {"kind": self.kind, "value": self.value}


def _valid_station(station_id: str) -> bool:
    if not station_id or len(station_id) > 32:
        return False
    return all(
        char.isascii() and (char.isupper() or char.isdigit() or char in "-_") for char in station_id
    )


def _valid_area(area: str) -> bool:
    return area in VALID_AREAS


@dataclass
class SearchCriteria:
    """検索条件の一式。範囲・放送日・フィルタをまとめ、保存と起動時復元に使う。"""

    scope: Scope
    range: BroadcastRange
    filter: Filter

    @classmethod
    def create(cls, scope: Scope, span: BroadcastRange, criteria_filter: Filter) -> SearchCriteria:
        if scope.kind == ScopeKind.STATION:
            if not isinstance(scope.value, str) or not _valid_station(scope.value):
                raise ValueError("保存する局IDが不正です")
        elif scope.kind == ScopeKind.AREAS:
            areas = list(scope.value or [])
            if not areas or any(not _valid_area(area) for area in areas):
                raise ValueError("保存する地域指定が不正です")
            scope = Scope.areas(sorted(set(areas)))
        elif scope.kind != ScopeKind.NATIONWIDE:
            raise ValueError("保存する地域指定が不正です")
        return cls(scope, span, criteria_filter)

    @classmethod
    def initial(cls, now: datetime) -> SearchCriteria:
        return cls.create(
            Scope.nationwide(), BroadcastRange.single(broadcast_day(now)), Filter.create()
        )

    def restore_startup(self, now: datetime) -> SearchCriteria:
        """起動時の復元は日付だけを当日1日へ戻す。"""
        return SearchCriteria.create(
            self.scope,
            BroadcastRange.single(broadcast_day(now)),
            self.filter,
        )

    def to_file_dict(self) -> dict:
        return {
            "scope": self.scope.to_dict(),
            "start": self.range.start.strftime("%Y%m%d"),
            "end": self.range.end.strftime("%Y%m%d"),
            "filter": self.filter.to_dict(),
        }

    def to_snapshot(self) -> dict:
        return self.to_file_dict()


def _reject_unknown(value: dict, allowed: set[str]) -> None:
    if set(value) - allowed:
        raise ValueError("unknown")


def criteria_from_file(value: dict) -> SearchCriteria:
    _reject_unknown(value, {"scope", "start", "end", "filter"})
    scope_value = value["scope"]
    if not isinstance(scope_value, dict) or set(scope_value) - {"kind", "value"}:
        raise ValueError("invalid")
    kind = scope_value.get("kind")
    if kind == "nationwide":
        if "value" in scope_value:
            raise ValueError("invalid")
        scope = Scope.nationwide()
    elif kind == "station":
        scope = Scope.station(scope_value["value"])
    elif kind == "areas":
        scope = Scope.areas(list(scope_value["value"]))
    else:
        raise ValueError("invalid")
    start = _ymd(value["start"])
    end = _ymd(value["end"])
    try:
        span = BroadcastRange.new(start, end)
    except ValueError as error:
        raise ValueError("invalid") from error
    try:
        return SearchCriteria.create(scope, span, Filter.from_dict(value["filter"]))
    except ValueError as error:
        raise ValueError("invalid") from error


def _ymd(text: str) -> date:
    if len(text) != 8 or not text.isdigit():
        raise ValueError("invalid")
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as error:
        raise ValueError("invalid") from error
