"""programs の引数解析と検索条件。CLI と TUI の両方がここを使う。"""

from __future__ import annotations

from datetime import datetime

from radiko.catalog import parse_day, validate_area
from radiko.domain import BroadcastRange, Filter, Scope, SearchCriteria, broadcast_day
from radiko.stream import validate_station

PROGRAMS_USAGE = (
    "programs STATION [START [END]] [--json] / programs (--areas JP13,JP14 | --all) [START [END]] [--json] "
    "[--genre IDS] [--personality IDS] [--keyword TEXT] [--genre-unclassified] [--personality-unclassified] "
    "[--classifications]（START・END: YYYYMMDD、両端を含む最大30放送日）"
)


class ProgramRequest:
    """programs の解析結果。局かエリア、放送日範囲、JSON・分類フラグ、フィルタを持つ。"""

    def __init__(self, station, areas, span, as_json, criteria_filter, classifications):
        """指定された値をそのまま保持する。"""
        self.station = station
        self.areas = areas
        self.range = span
        self.json = as_json
        self.filter = criteria_filter
        self.classifications = classifications


def value_arg(args: list[str], index: int, usage: str) -> str:
    """オプションの次の値を取る。値が無いか次の引数が -- なら usage エラー。"""
    if index + 1 >= len(args) or args[index + 1].startswith("--"):
        raise RuntimeError(usage)
    return args[index + 1]


def parse_program_request(args: list[str], now: datetime) -> ProgramRequest:
    """programs の引数を解析する。局指定か --areas/--all のどちらか、日付は最大2つ（既定は当日）、
    範囲は最大30放送日。不明なオプションは usage エラー、日付不正は CatalogError、範囲・フィルタ不正は RuntimeError。
    """
    genres = personalities = keyword = None
    unclassified_program = unclassified_personality = classifications = json_output = False
    areas = None
    positional: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--json" and not json_output:
            json_output = True
        elif arg == "--classifications" and not classifications:
            classifications = True
        elif arg == "--genre" and genres is None:
            genres = value_arg(args, index, PROGRAMS_USAGE).split(",")
            index += 1
        elif arg == "--personality" and personalities is None:
            personalities = value_arg(args, index, PROGRAMS_USAGE).split(",")
            index += 1
        elif arg == "--keyword" and keyword is None:
            keyword = value_arg(args, index, PROGRAMS_USAGE)
            index += 1
        elif arg == "--genre-unclassified" and not unclassified_program:
            unclassified_program = True
        elif arg == "--personality-unclassified" and not unclassified_personality:
            unclassified_personality = True
        elif arg == "--areas" and areas is None:
            raw = value_arg(args, index, PROGRAMS_USAGE)
            areas = raw.split(",")
            for area in areas:
                validate_area(area)
            index += 1
        elif arg == "--all" and areas is None:
            areas = [f"JP{number}" for number in range(1, 48)]
        elif arg.startswith("--"):
            raise RuntimeError(PROGRAMS_USAGE)
        else:
            positional.append(arg)
        index += 1
    if areas is None:
        if not positional:
            raise RuntimeError(PROGRAMS_USAGE)
        station = positional.pop(0)
        validate_station(station)
    else:
        station = None
    if len(positional) > 2:
        raise RuntimeError(PROGRAMS_USAGE)
    day = parse_day(positional[0]) if positional else broadcast_day(now)
    end = parse_day(positional[1]) if len(positional) > 1 else day
    try:
        span = BroadcastRange.new(day, end)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    try:
        criteria_filter = Filter.create(
            genres or [],
            personalities or [],
            unclassified_program,
            unclassified_personality,
            keyword,
        )
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    return ProgramRequest(station, areas or [], span, json_output, criteria_filter, classifications)


def criteria_from_request(request: ProgramRequest) -> SearchCriteria:
    """要求を SearchCriteria にする。局は station、47エリア全部は nationwide、それ以外は areas。"""
    if request.station:
        scope = Scope.station(request.station)
    # 47都道府県すべてが揃っている場合は、保存形式を全国（nationwide）に正規化する。
    elif len(set(request.areas)) == 47:
        scope = Scope.nationwide()
    else:
        scope = Scope.areas(request.areas)
    try:
        return SearchCriteria.create(scope, request.range, request.filter)
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def build_criteria(args: list[str], now: datetime) -> SearchCriteria:
    """引数を解析して検索条件にする。"""
    return criteria_from_request(parse_program_request(args, now))
