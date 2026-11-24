"""キャッシュしない局一覧と日別番組表。取得の並列は最大4。"""

from __future__ import annotations

import os
import re
import threading
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from radiko.domain import (
    JST,
    VALID_AREAS,
    BroadcastRange,
    Classification,
    Program,
    ProgramKey,
    Station,
    is_valid_station_id,
    parse_ymd,
)
from radiko.stream import ProbeError
from radiko.text import clean_text
from radiko.urls import trusted

MAX_SCHEDULE_BYTES = 4 * 1024 * 1024
MAX_PROGRAMS = 2048
MAX_STATIONS = 512
MAX_DEPTH = 32
WORKERS = 4
_FORBIDDEN_DECLARATION = re.compile(r"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class CatalogError(Exception):
    """番組表・局一覧の取得と解析の失敗。`code` と任意の `status` / `count` を持つ。

    `str()` が利用者向けの日本語の文言になる。
    """

    def __init__(self, code: str, status: int | None = None, count: int | None = None) -> None:
        """code・HTTP status・件数を保持し、表示文を例外メッセージにする。"""
        self.code = code
        self.status = status
        self.count = count
        super().__init__(str(self))

    def __str__(self) -> str:
        """code に対応する利用者向けの文言。`http` の 404 は専用の文、`empty` は
        不正・日付不一致の件数を含める。
        """
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
        """code・status・count が同じなら等しい。"""
        return isinstance(other, CatalogError) and (self.code, self.status, self.count) == (
            other.code,
            other.status,
            other.count,
        )


def validate_area(area: str) -> None:
    """地域IDが `VALID_AREAS`（JP1〜JP47）にあるか。無ければ CatalogError("area")。"""
    if area not in VALID_AREAS:
        raise CatalogError("area")


def validate_station(station: str) -> None:
    """局IDが空でなく32文字以内の ASCII 大文字・数字・-・_ だけか。
    違反は CatalogError("station")。
    """
    if not is_valid_station_id(station):
        raise CatalogError("station")


def parse_day(text: str) -> date:
    """`YYYYMMDD` を JST の日付にする。8桁でない・実在しない日付は CatalogError("date")。"""
    try:
        return parse_ymd(text)
    except ValueError as error:
        raise CatalogError("date") from error


def parse_clock(text: str) -> datetime:
    """`YYYYMMDDHHmmss` を JST の datetime にする。

    14桁の ASCII 数字でない・実在しない時刻は ValueError("bad")（CatalogError ではない）。
    """
    if len(text) != 14 or not text.isascii() or not text.isdigit():
        raise ValueError("bad")
    try:
        return datetime(
            int(text[0:4]),
            int(text[4:6]),
            int(text[6:8]),
            int(text[8:10]),
            int(text[10:12]),
            int(text[12:14]),
            tzinfo=JST,
        )
    except ValueError as error:
        raise ValueError("bad") from error


@dataclass
class ProgramList:
    """1局・1放送日の番組表。局名・番組・不正件数・重複件数を持つ。"""

    station_id: str
    station_name: str
    broadcast_day: date
    programs: list[Program]
    invalid_count: int = 0
    duplicate_count: int = 0


def parse_programs(data: bytes, station: str, day: date) -> ProgramList:
    """番組表 XML を検証し、`<prog>` を Program にして放送日順で返す。

    処理順: 局ID検証 → 4MB・XML構造（radiko/stations/station/progs）→ 2048枠上限 →
    JST 05:00 から翌日 05:00 の窓 → 各 `<prog>` の ft/to（不正は invalid_count）→
    (局, 開始) の重複（duplicate_count）→ title/pfm/desc/info は最初の要素だけ →
    tag は重複除去 → genre を番組/出演者に分類 → 開始時刻でソート。
    有効な枠が無く不正だけなら CatalogError("empty", count=invalid_count)。
    局不一致は wrong-station、構造不正は xml、上限超過は large。
    """
    validate_station(station)
    if len(data) > MAX_SCHEDULE_BYTES:
        raise CatalogError("large")
    try:
        text = data.decode()
    except UnicodeError as error:
        raise CatalogError("xml") from error
    root = _parse_document(text, "radiko")
    stations = [child for child in root if _local(child.tag) == "stations"]
    if len(stations) != 1:
        raise CatalogError("xml")
    station_nodes = [child for child in stations[0] if _local(child.tag) == "station"]
    if len(station_nodes) != 1:
        raise CatalogError("wrong-station")
    if station_nodes[0].attrib.get("id") != station:
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
        texts: dict[str, str] = {}
        genre = None
        tag_nodes = []
        for child in item:
            name = _local(child.tag)
            if name == "tag":
                tag_nodes.append(child)
            elif name == "genre":
                if genre is None:
                    genre = child
            elif name in {"title", "pfm", "desc", "info"} and name not in texts:
                texts[name] = child.text or ""
        title = clean_text(texts.get("title", "")) or "無題"
        tags = []
        seen_tags: set[str] = set()
        for tag in tag_nodes:
            for name in _child_texts(tag, "item", "name"):
                cleaned = clean_text(name)
                if cleaned and cleaned not in seen_tags:
                    seen_tags.add(cleaned)
                    tags.append(cleaned)
        program_genres, personality_genres = _genre_classes(genre)
        result.programs.append(
            Program(
                key=ProgramKey(station, starts_at),
                program_id=clean_text(item.attrib.get("id", "")),
                ends_at=ends_at,
                title=title,
                performer=clean_text(texts.get("pfm", "")),
                description=clean_text(texts.get("desc", "")),
                info=clean_text(texts.get("info", "")),
                tags=tags,
                program_genres=program_genres,
                personality_genres=personality_genres,
            )
        )
    if not result.programs and result.invalid_count:
        raise CatalogError("empty", count=result.invalid_count)
    result.programs.sort(key=lambda program: program.key.starts_at)
    return result


def parse_stations(data: bytes, area: str) -> list[Station]:
    """局一覧 XML を検証し、Station の並びを返す。

    ルートの `area_id` は指定地域と一致が必要（違反は wrong-area）。`<station>` が
    1件も無ければ xml、512件超は large。各局IDは validate_station、重複は最初だけ残す。
    """
    validate_area(area)
    if len(data) > MAX_SCHEDULE_BYTES:
        raise CatalogError("large")
    try:
        text = data.decode()
    except UnicodeError as error:
        raise CatalogError("xml") from error
    root = _parse_document(text, "stations")
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
    """配信先 XML から、モードと地域に合う `playlist_create_url` を返す。

    `areafree` と `timefree` が指定に一致する `<url>` だけを対象にし、URL は
    `trusted()` を通す（通らなければ ProbeError("unsafe")）。空の
    `playlist_create_url` の `<url>` は読み飛ばし、1件も残らなければ ValueError。
    重複は除く。XML 不正も ValueError。
    """
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
        if not target:
            continue
        # XML を信じる前に、再生に使うホストを確定する。Rust の endpoints() と同じ。
        if not trusted(target):
            raise ProbeError("unsafe")
        if target not in urls:
            urls.append(target)
    if not urls:
        raise ValueError("対象モードと地域に対応する配信先がありません")
    return urls


def _local(tag: str) -> str:
    """名前空間付きタグからローカル名だけを返す。"""
    return tag.rsplit("}", 1)[-1]


def _text(node: ET.Element, name: str) -> str:
    """指定名の最初の子要素のテキストを返す。無ければ空文字。"""
    for child in node:
        if _local(child.tag) == name:
            return child.text or ""
    return ""


def _child_texts(node: ET.Element, item_name: str, name: str) -> list[str]:
    """指定名の子要素すべてについて、その子の指定名テキストを並べて返す。"""
    values = []
    for item in node:
        if _local(item.tag) != item_name:
            continue
        values.append(_text(item, name))
    return values


def _genre_classes(genre: ET.Element | None) -> tuple[list[Classification], list[Classification]]:
    """`genre` の子を1パスで走査し、番組ジャンルと出演者分類を分けて返す。"""
    if genre is None:
        return [], []
    seen: dict[str, set[tuple[str, str]]] = {"program": set(), "personality": set()}
    results: dict[str, list[Classification]] = {"program": [], "personality": []}
    for child in genre:
        kind = _local(child.tag)
        if kind not in results:
            continue
        item = Classification(
            clean_text(child.attrib.get("id", "")), clean_text(_text(child, "name"))
        )
        if not item.id and not item.name:
            continue
        identity = (item.id, item.name)
        if identity in seen[kind]:
            continue
        seen[kind].add(identity)
        results[kind].append(item)
    return results["program"], results["personality"]


def _parse_document(text: str, root_name: str) -> ET.Element:
    """1回のパースで要素ツリー・ルート名・最大深さを検証する。"""
    # 巨大 XML や深い入れ子・エンティティ展開によるメモリ/スタック消費を
    # 全文展開前に遮断するため、宣言を拒否して 64KiB ずつ深さを検査する。
    if _FORBIDDEN_DECLARATION.search(text):
        raise CatalogError("xml")
    parser: ET.XMLPullParser = ET.XMLPullParser(events=("start", "end"))
    root: ET.Element | None = None
    depth = 0

    def consume(events) -> None:
        """start/end イベントでルート名と最大深さを検証する。"""
        nonlocal root, depth
        for event, element in events:
            if event == "start":
                if depth == 0:
                    if root is not None or _local(element.tag) != root_name:
                        raise CatalogError("xml")
                    root = element
                depth += 1
                if depth > MAX_DEPTH:
                    raise CatalogError("xml")
            else:
                depth -= 1
                if depth < 0:
                    raise CatalogError("xml")

    try:
        for start in range(0, len(text), 65536):
            parser.feed(text[start : start + 65536])
            consume(parser.read_events())
        consume(parser.read_events())
        parser.close()
    except ET.ParseError as error:
        raise CatalogError("xml") from error
    if root is None or depth != 0:
        raise CatalogError("xml")
    return root


def loopback_origin(value: str) -> str | None:
    """`http://127.0.0.1:port` 形式だけを許可して正規化する。

    スキーム・ホスト・ポート・パス（空か /）・クエリ・フラグメントを検査し、
    合わなければ None。
    """
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    return f"http://127.0.0.1:{parsed.port}"


def catalog_base() -> str:
    """番組表のベース URL。環境変数 `RADIKO_CATALOG_ORIGIN` がループバックならそれを使い、
    それ以外は `https://radiko.jp`。上書きが不正なら CatalogError("network")。
    """
    override = os.environ.get("RADIKO_CATALOG_ORIGIN")
    if override:
        origin = loopback_origin(override)
        if origin is None:
            raise CatalogError("network")
        return origin
    return "https://radiko.jp"


@dataclass
class SelectedStation:
    """局と、その局を返した地域の並び。"""

    station: Station
    areas: list[str]


@dataclass
class Failure:
    """取得に失敗した対象。地域なら day なし、番組表なら局と日付を持つ。"""

    target: str
    error: CatalogError
    day: date | None = None


@dataclass
class Report:
    """取得結果の集約。地域・局・番組表と、地域/番組表ごとの失敗を保持する。"""

    areas: list[str] = field(default_factory=list)
    stations: list[SelectedStation] = field(default_factory=list)
    lists: list[ProgramList] = field(default_factory=list)
    area_failures: list[Failure] = field(default_factory=list)
    schedule_failures: list[Failure] = field(default_factory=list)

    def complete(self) -> bool:
        """地域・番組表の失敗が無く、全 ProgramList の invalid_count が0なら True。
        重複件数（duplicate_count）は完全性の判定に含めない。
        """
        return (
            not self.area_failures
            and not self.schedule_failures
            and all(item.invalid_count == 0 for item in self.lists)
        )


@dataclass
class Progress:
    """進捗コールバックに渡す1件分。phase・対象・完了数・総数・失敗数など。"""

    phase: str
    target: str
    completed: int
    total: int
    failed: int
    day: date | None = None
    program_list: ProgramList | None = None


class CatalogClient:
    """fetch 関数で XML を取得し、局一覧と番組表を解析するクライアント。"""

    def __init__(self, fetch) -> None:
        """URL を受けて bytes を返す fetch を保持する。"""
        self.fetch = fetch

    def stations(self, area: str) -> list[Station]:
        """地域の局一覧 XML を取得して解析する。地域IDを先に検証する。"""
        validate_area(area)
        data = self.fetch(f"{catalog_base()}/v3/station/list/{area}.xml")
        return parse_stations(data, area)

    def programs(self, station: str, day: date) -> ProgramList:
        """局・放送日の番組表 XML を取得して解析する。局IDを先に検証する。"""
        validate_station(station)
        data = self.fetch(
            f"{catalog_base()}/v3/program/station/date/{day.strftime('%Y%m%d')}/{station}.xml"
        )
        return parse_programs(data, station, day)


def decode_body(data: bytes, encoding: str | None) -> bytes:
    """取得した本文を検査して返す。

    4MB 超は large。`identity` / 未指定はそのまま、`gzip` は上限付きで展開し、
    それ以外のエンコーディングは CatalogError("xml")。
    """
    if len(data) > MAX_SCHEDULE_BYTES:
        raise CatalogError("large")
    if encoding in {None, "identity"}:
        return data
    if encoding == "gzip":
        return _gunzip_bounded(data)
    raise CatalogError("xml")


def _gunzip_bounded(data: bytes) -> bytes:
    """上限（4MB）を超えた時点で展開を止める。ちょうど4MBまでは許す。"""
    if not data:
        raise CatalogError("xml")
    output = bytearray()
    remaining = data
    # 連結された複数の gzip メンバーにも対応しつつ、
    # gzip ボムを全展開前に 4MiB 上限で即座に打ち切る。
    while remaining:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        pending = remaining
        while True:
            room = MAX_SCHEDULE_BYTES + 1 - len(output)
            if room <= 0:
                raise CatalogError("large")
            try:
                chunk = decompressor.decompress(pending, min(room, 1024 * 1024))
            except zlib.error as error:
                raise CatalogError("xml") from error
            if len(output) + len(chunk) > MAX_SCHEDULE_BYTES:
                raise CatalogError("large")
            output.extend(chunk)
            pending = decompressor.unconsumed_tail
            if decompressor.eof:
                remaining = decompressor.unused_data
                break
            if not pending:
                raise CatalogError("xml")
    return bytes(output)


def acquire_range(
    source, areas: list[str], span: BroadcastRange, cancelled: threading.Event, progress
) -> Report:
    """地域の並びから局一覧を並列取得し、続けて全員分の番組表を取得する。

    地域IDは重複を除いてソートし、空なら CatalogError("area")。局一覧は最大4並列で、
    CatalogError は area_failures に積んで続行、それ以外の例外は送出する。同じ局が
    複数地域にある場合は areas にまとめる。番組表は `_schedules` が担当。
    各段階の前後と `_schedules` 内で cancelled を確認し、取り消しは CatalogError("cancelled")。
    """
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
        """1地域の局一覧を取得する。取り消し済みなら CatalogError("cancelled")。"""
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
    report.stations = sorted(selected.values(), key=lambda item: item.station.id)
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
    """局IDの並びから全員分の番組表を並列取得する。

    空なら CatalogError("station")、各IDは validate_station。局名は取得できた
    ProgramList の station_name で補う。取り消しは CatalogError("cancelled")。
    """
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
    by_station = {item.station_id: item for item in report.lists}
    for entry in report.stations:
        listing = by_station.get(entry.station.id)
        if listing is not None:
            entry.station = Station(entry.station.id, listing.station_name)
    return report


def _schedules(
    source, span: BroadcastRange, report: Report, cancelled: threading.Event, progress
) -> None:
    """局×放送日の番組表を最大4並列で取得し、report に詰める。

    CatalogError は schedule_failures に積んで続行し、それ以外の例外は送出する。
    各件で progress を呼び、lists・area_failures・schedule_failures をソートする。
    取り消されたら残りを打ち切る。
    """
    jobs = [(station.station.id, day) for station in report.stations for day in span.days]
    failed = 0

    def one(job):
        """1局・1日の番組表を取得する。取り消し済みなら CatalogError("cancelled")。"""
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
    report.lists.sort(key=lambda item: (item.station_id, item.broadcast_day))
    report.area_failures.sort(key=lambda item: item.target)
    report.schedule_failures.sort(
        key=lambda item: (item.target, item.day.isoformat() if item.day else "")
    )


def _parallel(items, function, cancelled: threading.Event):
    """items を最大4並列で function に渡し、(item, 結果) の並びを返す。

    各スロットの CatalogError とその他の例外は結果として保持し、送出は呼び出し側が行う。
    取り消し済みのスロットには CatalogError("cancelled") を入れる。
    """
    if not items:
        return []
    results: list = [None] * len(items)

    def run(index: int, item):
        """1件を実行し、結果か例外を自分のスロットに格納する。"""
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
