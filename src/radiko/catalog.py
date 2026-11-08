"""キャッシュしない局一覧と日別番組表。取得の並列は最大4。"""

from __future__ import annotations

import gzip
import os
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from radiko.domain import (
    JST,
    BroadcastRange,
    Classification,
    Program,
    ProgramKey,
    Station,
)
from radiko.text import clean_text

MAX_SCHEDULE_BYTES = 4 * 1024 * 1024
MAX_PROGRAMS = 2048
MAX_STATIONS = 512
MAX_DEPTH = 32
WORKERS = 4


class CatalogError(Exception):
    def __init__(self, code: str, status: int | None = None, count: int | None = None) -> None:
        self.code = code
        self.status = status
        self.count = count
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.code == "cancelled":
            return "検索を取り消しました"
        if self.code == "area":
            return "地域IDが不正です（JP1〜JP47）"
        if self.code == "wrong-area":
            return "取得した局一覧が指定した地域と一致しません"
        if self.code == "station":
            return "局IDが不正です（例: TBS、OBS、FMT）"
        if self.code == "date":
            return "放送日が不正です（YYYYMMDDで指定してください）"
        if self.code == "network":
            return "番組表サーバーと通信できません"
        if self.code == "http" and self.status == 404:
            return "指定した局・放送日の番組表が見つかりません"
        if self.code == "http":
            return f"番組表サーバーがHTTP {self.status}を返しました"
        if self.code == "limited":
            return "番組表へのアクセス制限中です。時間をおいて再実行してください"
        if self.code == "large":
            return "番組表のサイズまたは番組件数の上限を超えました"
        if self.code == "xml":
            return "番組表XMLの構造が不正です"
        if self.code == "wrong-station":
            return "取得した番組表が指定した局と一致しません"
        if self.code == "empty":
            return f"番組表に指定日の有効な放送枠がありません（不正・日付不一致: {self.count}件）"
        return "番組表サーバーと通信できません"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CatalogError) and (self.code, self.status, self.count) == (
            other.code,
            other.status,
            other.count,
        )


def validate_area(area: str) -> None:
    if not any(area == f"JP{number}" for number in range(1, 48)):
        raise CatalogError("area")


def validate_station(station: str) -> None:
    if (
        not station
        or len(station) > 32
        or not all(
            char.isascii() and (char.isupper() or char.isdigit() or char in "-_")
            for char in station
        )
    ):
        raise CatalogError("station")


def parse_day(text: str) -> date:
    if len(text) != 8 or not text.isdigit():
        raise CatalogError("date")
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as error:
        raise CatalogError("date") from error


def parse_clock(text: str) -> datetime:
    if len(text) != 14 or not text.isdigit():
        raise ValueError("bad")
    try:
        parsed = datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=JST)
    except ValueError as error:
        raise ValueError("bad") from error
    return parsed


@dataclass
class ProgramList:
    station_id: str
    station_name: str
    broadcast_day: date
    programs: list[Program]
    invalid_count: int = 0
    duplicate_count: int = 0


def parse_programs(data: bytes, station: str, day: date) -> ProgramList:
    validate_station(station)
    if len(data) > MAX_SCHEDULE_BYTES:
        raise CatalogError("large")
    try:
        text = data.decode()
    except UnicodeError as error:
        raise CatalogError("xml") from error
    _validate_xml(text, "radiko")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise CatalogError("xml") from error
    stations = [child for child in root if _local(child.tag) == "stations"]
    if len(stations) != 1:
        raise CatalogError("xml")
    station_nodes = [child for child in stations[0] if _local(child.tag) == "station"]
    if len(station_nodes) != 1 or station_nodes[0].attrib.get("id") != station:
        raise CatalogError("wrong-station")
    selected = station_nodes[0]
    progs = [child for child in selected if _local(child.tag) == "progs"]
    if len(progs) != 1:
        raise CatalogError("xml")
    items = [child for child in progs[0] if _local(child.tag) == "prog"]
    if len(items) > MAX_PROGRAMS:
        raise CatalogError("large")
    start = datetime(day.year, day.month, day.day, 5, 0, 0, tzinfo=JST)
    end = start + timedelta(days=1)
    result = ProgramList(station, clean_text(_text(selected, "name")), day, [])
    seen: set[tuple[str, datetime]] = set()
    for item in items:
        try:
            starts_at = parse_clock(item.attrib.get("ft", ""))
            ends_at = parse_clock(item.attrib.get("to", ""))
        except ValueError:
            result.invalid_count += 1
            continue
        if ends_at <= starts_at or starts_at >= end or ends_at <= start:
            result.invalid_count += 1
            continue
        identity = (station, starts_at)
        if identity in seen:
            result.duplicate_count += 1
            continue
        seen.add(identity)
        title = clean_text(_text(item, "title")) or "無題"
        tags = []
        seen_tags: set[str] = set()
        for tag in _descendants(item, "tag"):
            for name in _child_texts(tag, "item", "name"):
                cleaned = clean_text(name)
                if cleaned and cleaned not in seen_tags:
                    seen_tags.add(cleaned)
                    tags.append(cleaned)
        genre = next((child for child in item if _local(child.tag) == "genre"), None)
        result.programs.append(
            Program(
                key=ProgramKey(station, starts_at),
                program_id=clean_text(item.attrib.get("id", "")),
                ends_at=ends_at,
                title=title,
                performer=clean_text(_text(item, "pfm")),
                description=clean_text(_text(item, "desc")),
                info=clean_text(_text(item, "info")),
                tags=tags,
                program_genres=_classes(genre, "program"),
                personality_genres=_classes(genre, "personality"),
            )
        )
    if not result.programs and result.invalid_count:
        raise CatalogError("empty", count=result.invalid_count)
    result.programs.sort(key=lambda program: program.key.starts_at)
    return result


def parse_stations(data: bytes, area: str) -> list[Station]:
    validate_area(area)
    if len(data) > MAX_SCHEDULE_BYTES:
        raise CatalogError("large")
    try:
        text = data.decode()
    except UnicodeError as error:
        raise CatalogError("xml") from error
    _validate_xml(text, "stations")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise CatalogError("xml") from error
    if root.attrib.get("area_id") != area:
        raise CatalogError("wrong-area")
    nodes = [child for child in root if _local(child.tag) == "station"]
    if len(nodes) > MAX_STATIONS:
        raise CatalogError("large")
    if not nodes:
        raise CatalogError("xml")
    seen: set[str] = set()
    stations = []
    for item in nodes:
        station_id = _text(item, "id")
        try:
            validate_station(station_id)
        except CatalogError as error:
            raise CatalogError("station") from error
        if station_id in seen:
            continue
        seen.add(station_id)
        stations.append(Station(station_id, clean_text(_text(item, "name"))))
    return stations


def parse_endpoints(data: bytes, timefree: bool, outside: bool) -> list[str]:
    try:
        root = ET.fromstring(data.decode())
    except (UnicodeError, ET.ParseError) as error:
        raise ValueError("局または配信設定XMLの形式が不正です") from error
    urls = []
    for node in root:
        if _local(node.tag) != "url":
            continue
        if node.attrib.get("areafree") != ("1" if outside else "0"):
            continue
        if node.attrib.get("timefree") != ("1" if timefree else "0"):
            continue
        target = _text(node, "playlist_create_url")
        if target and target not in urls:
            urls.append(target)
    if not urls:
        raise ValueError("対象モードと地域に対応する配信先がありません")
    return urls


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node: ET.Element, name: str) -> str:
    child = next((item for item in node if _local(item.tag) == name), None)
    if child is None or child.text is None:
        return ""
    return child.text


def _child_texts(node: ET.Element, item_name: str, name: str) -> list[str]:
    values = []
    for item in node:
        if _local(item.tag) != item_name:
            continue
        values.append(_text(item, name))
    return values


def _descendants(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _local(child.tag) == name]


def _classes(genre: ET.Element | None, kind: str) -> list[Classification]:
    if genre is None:
        return []
    seen: set[tuple[str, str]] = set()
    result = []
    for child in genre:
        if _local(child.tag) != kind:
            continue
        item = Classification(
            clean_text(child.attrib.get("id", "")), clean_text(_text(child, "name"))
        )
        if not item.id and not item.name:
            continue
        identity = (item.id, item.name)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


def _validate_xml(text: str, root_name: str) -> None:
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise CatalogError("xml")
    depth = 0
    seen_root = False
    index = 0
    while True:
        start = text.find("<", index)
        if start < 0:
            break
        if text.startswith("<?", start) or text.startswith("<!", start):
            end = text.find(">", start)
            if end < 0:
                raise CatalogError("xml")
            index = end + 1
            continue
        closing = text.startswith("</", start)
        end = text.find(">", start)
        if end < 0:
            raise CatalogError("xml")
        empty = text[end - 1] == "/"
        name = text[start + (2 if closing else 1) : end].split()[0].strip("/")
        name = name.rsplit("}", 1)[-1]
        if closing:
            depth -= 1
            if depth < 0:
                raise CatalogError("xml")
        else:
            if depth == 0:
                if seen_root or name != root_name:
                    raise CatalogError("xml")
                seen_root = True
            if not empty:
                depth += 1
                if depth > MAX_DEPTH:
                    raise CatalogError("xml")
        index = end + 1
    if not seen_root or depth != 0:
        raise CatalogError("xml")


def loopback_origin(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    return f"http://127.0.0.1:{parsed.port}"


def catalog_base() -> str:
    override = os.environ.get("RADIKO_CATALOG_ORIGIN")
    if override:
        origin = loopback_origin(override)
        if origin is None:
            raise CatalogError("network")
        return origin
    return "https://radiko.jp"


@dataclass
class SelectedStation:
    station: Station
    areas: list[str]


@dataclass
class Failure:
    target: str
    error: CatalogError
    day: date | None = None


@dataclass
class Report:
    areas: list[str] = field(default_factory=list)
    stations: list[SelectedStation] = field(default_factory=list)
    lists: list[ProgramList] = field(default_factory=list)
    area_failures: list[Failure] = field(default_factory=list)
    schedule_failures: list[Failure] = field(default_factory=list)

    def complete(self) -> bool:
        return (
            not self.area_failures
            and not self.schedule_failures
            and all(item.invalid_count == 0 for item in self.lists)
        )


@dataclass
class Progress:
    phase: str
    target: str
    completed: int
    total: int
    failed: int
    day: date | None = None
    program_list: ProgramList | None = None


class CatalogClient:
    def __init__(self, fetch) -> None:
        self.fetch = fetch

    def stations(self, area: str) -> list[Station]:
        validate_area(area)
        data = self.fetch(f"{catalog_base()}/v3/station/list/{area}.xml")
        return parse_stations(data, area)

    def programs(self, station: str, day: date) -> ProgramList:
        validate_station(station)
        data = self.fetch(
            f"{catalog_base()}/v3/program/station/date/{day.strftime('%Y%m%d')}/{station}.xml"
        )
        return parse_programs(data, station, day)


def decode_body(data: bytes, encoding: str | None) -> bytes:
    if len(data) > MAX_SCHEDULE_BYTES:
        raise CatalogError("large")
    if encoding in {None, "identity"}:
        return data
    if encoding == "gzip":
        try:
            output = gzip.decompress(data)
        except (OSError, EOFError, gzip.BadGzipFile) as error:
            raise CatalogError("xml") from error
        if len(output) > MAX_SCHEDULE_BYTES:
            raise CatalogError("large")
        return output
    raise CatalogError("xml")


def acquire_range(
    source, areas: list[str], span: BroadcastRange, cancelled: threading.Event, progress
) -> Report:
    if cancelled.is_set():
        raise CatalogError("cancelled")
    for area in areas:
        validate_area(area)
    if not areas:
        raise CatalogError("area")
    unique = sorted(set(areas))
    report = Report(areas=unique)
    selected: dict[str, SelectedStation] = {}
    failed = 0

    def one(area: str):
        if cancelled.is_set():
            raise CatalogError("cancelled")
        return source.stations(area)

    results = _parallel(unique, one, cancelled)
    for index, (area, result) in enumerate(results, start=1):
        if isinstance(result, CatalogError):
            report.area_failures.append(Failure(area, result))
            failed += 1
        elif isinstance(result, Exception):
            raise result
        else:
            for station in result:
                entry = selected.setdefault(station.id, SelectedStation(station, []))
                if area not in entry.areas:
                    entry.areas.append(area)
        progress(Progress("局一覧", area, index, len(unique), failed))
    report.stations = list(selected.values())
    for station in report.stations:
        station.areas.sort()
    if cancelled.is_set():
        raise CatalogError("cancelled")
    _schedules(source, span, report, cancelled, progress)
    if cancelled.is_set():
        raise CatalogError("cancelled")
    return report


def acquire_stations(
    source, stations: list[str], span: BroadcastRange, cancelled: threading.Event, progress
) -> Report:
    if cancelled.is_set():
        raise CatalogError("cancelled")
    if not stations:
        raise CatalogError("station")
    for station in stations:
        validate_station(station)
    unique = sorted(set(stations))
    report = Report(stations=[SelectedStation(Station(station, station), []) for station in unique])
    _schedules(source, span, report, cancelled, progress)
    if cancelled.is_set():
        raise CatalogError("cancelled")
    return report


def _schedules(
    source, span: BroadcastRange, report: Report, cancelled: threading.Event, progress
) -> None:
    jobs = [(station.station.id, day) for station in report.stations for day in span.days]
    failed = len(report.area_failures)

    def one(job):
        station, day = job
        if cancelled.is_set():
            raise CatalogError("cancelled")
        return source.programs(station, day)

    results = _parallel(jobs, one, cancelled)
    for index, ((station, day), result) in enumerate(results, start=1):
        if isinstance(result, CatalogError):
            report.schedule_failures.append(Failure(station, result, day))
            failed += 1
            shown = None
        elif isinstance(result, Exception):
            raise result
        else:
            report.lists.append(result)
            shown = result
        progress(Progress("番組表", station, index, len(jobs), failed, day, shown))
        if cancelled.is_set():
            return


def _parallel(items, function, cancelled: threading.Event):
    if not items:
        return []
    results: list = [None] * len(items)

    def run(index: int, item):
        if cancelled.is_set():
            results[index] = CatalogError("cancelled")
            return
        try:
            results[index] = function(item)
        except CatalogError as error:
            results[index] = error
        except Exception as error:  # noqa: BLE001 - keep the job's failure on its slot
            results[index] = error

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as pool:
        futures = [pool.submit(run, index, item) for index, item in enumerate(items)]
        for future in futures:
            future.result()
    return list(zip(items, results, strict=True))
