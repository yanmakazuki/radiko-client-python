"""引数、終了コード、各サブコマンド。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from radiko.catalog import (
    CatalogClient,
    ProgramList,
    acquire_range,
    acquire_stations,
    parse_day,
    validate_area,
)
from radiko.chapters import parse_chapters
from radiko.domain import (
    STATUS_LABEL,
    BroadcastRange,
    Filter,
    ObservedClassifications,
    Scope,
    ScopeKind,
    SearchCriteria,
    broadcast_day,
    broadcast_status,
    rfc3339,
)
from radiko.http_client import catalog_fetch
from radiko.output import (
    OutputError,
    inspect_default_output,
    probe_silent,
    probe_tone,
)
from radiko.storage import (
    DataDir,
    SearchError,
    SearchStore,
    SessionStore,
    StorageError,
)
from radiko.stream import ProbeError, parse_time, validate_station
from radiko_client.io import ClosedPipe, stderr_ln, stdout_ln
from radiko_client.tui import run as tui_run

TOP = (
    "radiko-client: 検証用CLI・検索／ライブ・タイムフリー再生TUI\n\n"
    "  radiko-client [--data-dir DIR] login\n"
    "  radiko-client [--data-dir DIR] session\n"
    "  radiko-client [--data-dir DIR] logout\n"
    "  radiko-client [--data-dir DIR] probe-live STATION\n"
    "  radiko-client [--data-dir DIR] probe-timefree STATION YYYYMMDDHHmmss\n"
    "  radiko-client [--data-dir DIR] play-live STATION [--interactive] [--seconds N]\n"
    "  radiko-client [--data-dir DIR] play-timefree STATION YYYYMMDDHHmmss [--offset SECONDS] [--interactive] [--seconds N]\n"
    "  radiko-client output-info [SAMPLE_RATE]\n"
    "  radiko-client probe-output [--mute] [SECONDS]\n"
    "  radiko-client [--data-dir DIR] play-sample STATION [YYYYMMDDHHmmss]\n"
    "  radiko-client programs STATION [START [END]] [--json]\n"
    "  radiko-client programs (--areas JP13,JP14 | --all) [START [END]] [--json]\n"
    "  radiko-client programs --help（絞り込みオプション）\n"
    "  radiko-client tui [programsと同じ検索条件]\n"
    "  radiko-client searches --help（条件の保存・一覧・削除）\n"
    "  radiko-client programs --saved NAME / --restore\n"
    "  radiko-client chapters STATION YYYYMMDDHHmmss\n"
    "  radiko-client inspect-chapters FILE SLOT_SECONDS VERIFIED_OFFSET_SECONDS\n\n"
    "loginは端末でメールアドレスと非表示のパスワードを入力します。\n"
    "sessionは保存済みセッションの有効性と契約を確認します。\n"
    "logoutはローカルの保存済みセッションを削除します。\n"
    "inspect-chaptersはローカルJSONを解析し、聴取権限は確認しません。"
)
PROGRAMS_USAGE = (
    "programs STATION [START [END]] [--json] / programs (--areas JP13,JP14 | --all) [START [END]] [--json] "
    "[--genre IDS] [--personality IDS] [--keyword TEXT] [--genre-unclassified] [--personality-unclassified] "
    "[--classifications]（START・END: YYYYMMDD、両端を含む最大30放送日）"
)
PROGRAMS_HELP = (
    PROGRAMS_USAGE
    + "\nprograms --saved NAME [--json] [--classifications] / programs --restore [--json] [--classifications]"
)
SEARCHES_USAGE = (
    "searches list / searches show NAME / searches restore / searches save NAME "
    "<programsの局・地域・日付・絞り込み> / searches delete NAME（通信なし）"
)
PLAY_USAGE = (
    "play-live STATION [--interactive] [--seconds N] / play-timefree STATION YYYYMMDDHHmmss "
    "[--offset SECONDS] [--interactive] [--seconds N]（N: 1〜86400、未指定は番組末尾まで）"
)
CHAPTERS_USAGE = "chapters STATION YYYYMMDDHHmmss（日本時間、取得・一覧表示のみ）"


def main(argv: list[str] | None = None) -> int:
    try:
        run(sys.argv[1:] if argv is None else argv)
    except (ClosedPipe, BrokenPipeError):
        _silence_stdout()
        return 141
    except Exception as error:
        stderr_ln(f"エラー: {error}")
        return 1
    return 0


def _silence_stdout() -> None:
    """終了時の flush が壊れたパイプへ届き、141 が 120 に置き換わるのを防ぐ。"""
    import os

    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    except OSError:
        return


def run(argv: list[str]) -> None:
    args, data_dir = split_data_dir(argv)
    if not args or args == ["--help"]:
        stdout_ln(TOP)
        return
    command = args[0]
    if command == "tui":
        tui_run(args[1:], data_dir)
        return
    if command == "searches":
        searches(args[1:], data_dir)
        return
    if command == "chapters":
        chapters(args[1:])
        return
    if command in {"play-live", "play-timefree"}:
        play(args, data_dir)
        return
    if command == "output-info":
        output_info(args)
        return
    if command == "probe-output":
        probe_output(args)
        return
    if command == "programs":
        programs(args[1:], data_dir)
        return
    if len(args) == 1 and command in {"login", "session", "logout"}:
        member_command(command, _session_store(data_dir))
        return
    if (
        (len(args) == 2 and command == "probe-live")
        or (len(args) == 3 and command == "probe-timefree")
        or (command == "play-sample" and len(args) in {2, 3})
    ):
        probe_media(args, data_dir)
        return
    if len(args) == 4 and command == "inspect-chapters":
        inspect_chapters(args)
        return
    raise RuntimeError("引数が不正です。--helpを参照してください")


def split_data_dir(args: list[str]) -> tuple[list[str], Path | None]:
    data_dir = None
    kept: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == "--data-dir":
            if (
                data_dir is not None
                or index + 1 >= len(args)
                or args[index + 1] == ""
                or args[index + 1].startswith("--")
            ):
                raise RuntimeError("--data-dirには保存先を1回だけ指定してください")
            data_dir = Path(args[index + 1])
            index += 2
        else:
            kept.append(args[index])
            index += 1
    return kept, data_dir


def _session_store(data_dir: Path | None) -> SessionStore:
    if data_dir is not None:
        return SessionStore(data_dir)
    return SessionStore.default()


def _search_store(data_dir: Path | None) -> SearchStore:
    if data_dir is not None:
        return SearchStore(data_dir)
    return SearchStore(DataDir.default().directory)


def inspect_chapters(args: list[str]) -> None:
    if args[2] != args[2].strip():
        raise RuntimeError("放送枠の秒数が不正です")
    try:
        seconds = float(args[2])
    except ValueError as error:
        raise RuntimeError("放送枠の秒数が不正です") from error
    if seconds != seconds or abs(seconds) == float("inf") or seconds < 0:
        raise RuntimeError("放送枠の秒数が不正です")
    if args[3] != args[3].strip():
        raise RuntimeError("補正秒数が不正です")
    try:
        offset = float(args[3])
    except ValueError as error:
        raise RuntimeError("補正秒数が不正です") from error
    if offset != offset or abs(offset) == float("inf"):
        raise RuntimeError("補正秒数が不正です")
    path = Path(args[1])
    try:
        data = path.read_bytes()[: 1024 * 1024 + 1]
    except OSError as error:
        message = error.strerror or str(error)
        raise RuntimeError(f"{message} (os error {error.errno})") from error
    chapter_set = parse_chapters(data, seconds, offset)
    if not chapter_set.chapters:
        stdout_ln("チャプター未提供")
    for chapter in chapter_set.chapters:
        stdout_ln(f"{chapter.start:.3f}–{chapter.end:.3f}秒  {chapter.title}")
    stdout_ln(f"不正項目: {chapter_set.invalid_count} / 除外した楽曲: {chapter_set.music_count}")


def _sigint_stop(stop):
    import signal

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_args: stop.set())
    return previous


def _restore_sigint(previous) -> None:
    import signal

    signal.signal(signal.SIGINT, previous)


def member_command(command: str, store: SessionStore) -> None:
    if command == "logout":
        store.delete()
        stdout_ln("ローカルのセッションを削除しました")
        return
    try:
        saved = store.load()
    except StorageError as error:
        stderr_ln(f"{error}。既存ファイルを保持して未ログインとして扱います")
        saved = None
    if command == "session":
        if saved is None:
            stdout_ln("未ログインです。loginでログインしてください")
            return
        from radiko.auth import MemberClient

        membership = MemberClient.create().check(saved)
        _print_membership(membership)
        return
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise RuntimeError("loginは対話端末で実行してください")
    stderr_ln("メールアドレス: ")
    mail = sys.stdin.readline()
    import getpass

    password = getpass.getpass("パスワード（非表示）: ")
    try:
        from radiko.auth import MemberClient

        session, membership = MemberClient.create().login(mail.strip(), password)
    finally:
        password = "\0" * len(password)
        del password
    store.save(session)
    stdout_ln("ログインし、セッションを保存しました")
    _print_membership(membership)


def _print_membership(membership) -> None:
    def label(value):
        if value is True:
            return "あり"
        if value is False:
            return "なし"
        return "未確認"

    stdout_ln(
        f"ログイン有効 / エリアフリー: {label(membership.area_free)} / タイムフリー30: {label(membership.time_free_30)}"
    )
    stdout_ln("個別番組の再生可否は未確認です")


def output_info(args: list[str]) -> None:
    usage = "output-info [SAMPLE_RATE]（8000〜192000Hz・既定48000Hz、出力機器の読み取りのみ）"
    if args == ["output-info", "--help"]:
        stdout_ln(usage)
        return
    if len(args) > 2:
        raise RuntimeError(usage)
    if len(args) == 1:
        rate = 48000
    else:
        raw = args[1]
        body = raw[1:] if raw.startswith("+") else raw
        if not body or not body.isascii() or not body.isdigit():
            raise RuntimeError("サンプルレートは正の整数Hzで指定してください")
        try:
            rate = int(body)
        except ValueError as error:
            raise RuntimeError("サンプルレートは正の整数Hzで指定してください") from error
        if rate > 0xFFFFFFFF:
            raise RuntimeError("サンプルレートは正の整数Hzで指定してください")
    if not 8000 <= rate <= 192000:
        raise RuntimeError("サンプルレートは8000〜192000Hzで指定してください")
    try:
        info = inspect_default_output(rate)
    except OutputError as error:
        raise RuntimeError(str(error)) from error
    stdout_ln(f"現在の既定出力機器: {info.name}")
    if info.sample_format and info.channels:
        stdout_ln(
            f"{info.sample_rate}Hz / {info.sample_format} / {info.channels}ch: 対応宣言あり（再生時に再確認）"
        )
    else:
        stdout_ln(info.mismatch)


def probe_output(args: list[str]) -> None:
    if len(args) == 2 and args[1] == "--help":
        stdout_ln(
            "probe-output [SECONDS]（生成音・1〜10秒・既定2秒）\n"
            "probe-output --mute [SECONDS]（無音・1〜1800秒・既定30秒、Ctrl+Cで中断）"
        )
        return
    muted = len(args) > 1 and args[1] == "--mute"
    values = args[2:] if muted else args[1:]
    if len(values) > 1:
        raise RuntimeError("probe-output [--mute] [SECONDS]（--helpで範囲を確認）")
    if values:
        seconds = _parse_u32(values[0])
    else:
        seconds = 30 if muted else 2
    import threading

    stop = threading.Event()
    if muted:
        if not 1 <= seconds <= 1800:
            raise RuntimeError("無音の出力診断は1〜1800秒で指定してください")
        previous = _sigint_stop(stop)
        try:
            summary = probe_silent(
                seconds,
                stop,
                on_progress=lambda value: stderr_ln(
                    f"無音出力中: {value.frames}フレーム / {value.sample_rate}Hz / "
                    f"{value.sample_format} / 不足{value.underrun_frames}フレーム"
                ),
            )
        finally:
            _restore_sigint(previous)
        stdout_ln(
            f"無音出力{'中断' if stop.is_set() else '完了'}: {summary.frames}フレーム / {summary.sample_rate}Hz / "
            f"{summary.sample_format} / {summary.channels}ch / バッファ不足: {summary.underrun_frames}フレーム"
        )
        return
    try:
        summary = probe_tone(seconds)
    except OutputError as error:
        raise RuntimeError(str(error)) from error
    stdout_ln(
        f"生成音の出力完了: {summary.frames}フレーム / {summary.sample_rate}Hz / {summary.sample_format} / "
        f"{summary.channels}ch / バッファ不足: {summary.underrun_frames}フレーム"
    )


def _parse_u32(text: str) -> int:
    """Rust の parse::<u32>() と同じ失敗文にする。"""
    if text == "":
        raise RuntimeError("cannot parse integer from empty string")
    body = text[1:] if text.startswith("+") else text
    if not body or not body.isascii() or not body.isdigit():
        raise RuntimeError("invalid digit found in string")
    value = int(body)
    if value > 0xFFFFFFFF:
        raise RuntimeError("number too large to fit in target type")
    return value


def searches(args: list[str], data_dir: Path | None) -> None:
    if args == ["--help"]:
        stdout_ln(SEARCHES_USAGE)
        return
    command = args[0] if args else ""
    valid = (
        (command in {"list", "restore"} and len(args) == 1)
        or (command in {"show", "delete"} and len(args) == 2)
        or (command == "save" and len(args) >= 3)
    )
    if not valid:
        raise RuntimeError(SEARCHES_USAGE)
    now = datetime.now(timezone.utc)
    criteria = None
    if command == "save":
        if any(arg in {"--json", "--classifications"} for arg in args[2:]):
            raise RuntimeError("保存操作には表示オプションを指定しないでください")
        criteria = build_criteria(args[2:], now)
    store = _search_store(data_dir)
    if command == "list":
        for name in sorted(store.load().named):
            stdout_ln(name)
    elif command == "show":
        library = store.load()
        if args[1] not in library.named:
            raise SearchError("missing")
        stdout_ln(
            json.dumps(
                library.named[args[1]].to_snapshot(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    elif command == "restore":
        try:
            library = store.load()
            chosen = (
                library.last.restore_startup(now) if library.last else SearchCriteria.initial(now)
            )
        except SearchError as error:
            stderr_ln(f"検索条件を復元できません: {error}（当日・全国・指定なしを使用）")
            chosen = SearchCriteria.initial(now)
        stdout_ln(json.dumps(chosen.to_snapshot(), ensure_ascii=False, indent=2, sort_keys=True))
    elif command == "save":
        assert criteria is not None
        store.save_named(args[1], criteria)
        stdout_ln("名前付き検索条件を保存しました")
    elif command == "delete":
        store.delete_named(args[1])
        stdout_ln("名前付き検索条件を削除しました")


def programs(args: list[str], data_dir: Path | None) -> None:
    if args == ["--help"]:
        stdout_ln(PROGRAMS_HELP)
        return
    now = datetime.now(timezone.utc)
    restoring = bool(args) and args[0] in {"--saved", "--restore"}
    store = None
    try:
        store = _search_store(data_dir)
    except StorageError as error:
        stderr_ln(f"検索条件を保存できません: {error}")
    if restoring:
        request, allow_save = restored_request(args, now, store)
    else:
        request = parse_program_request(args, now)
        allow_save = True
    if allow_save and store is not None:
        try:
            store.save_last(criteria_from_request(request))
        except SearchError as error:
            stderr_ln(f"検索条件を保存できません: {error}（現在の条件で検索を継続）")
    if request.station and len(request.range.days) == 1:
        _print_station_day(request, now)
        return
    _acquire(request, now)


class ProgramRequest:
    def __init__(self, station, areas, span, as_json, criteria_filter, classifications):
        self.station = station
        self.areas = areas
        self.range = span
        self.json = as_json
        self.filter = criteria_filter
        self.classifications = classifications


def parse_program_request(args: list[str], now: datetime) -> ProgramRequest:
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
            genres = _value(args, index, PROGRAMS_USAGE).split(",")
            index += 1
        elif arg == "--personality" and personalities is None:
            personalities = _value(args, index, PROGRAMS_USAGE).split(",")
            index += 1
        elif arg == "--keyword" and keyword is None:
            keyword = _value(args, index, PROGRAMS_USAGE)
            index += 1
        elif arg == "--genre-unclassified" and not unclassified_program:
            unclassified_program = True
        elif arg == "--personality-unclassified" and not unclassified_personality:
            unclassified_personality = True
        elif arg == "--areas" and areas is None:
            raw = _value(args, index, PROGRAMS_USAGE)
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


def _value(args: list[str], index: int, usage: str) -> str:
    if index + 1 >= len(args) or args[index + 1].startswith("--"):
        raise RuntimeError(usage)
    return args[index + 1]


def criteria_from_request(request: ProgramRequest) -> SearchCriteria:
    if request.station:
        scope = Scope.station(request.station)
    elif len(set(request.areas)) == 47:
        scope = Scope.nationwide()
    else:
        scope = Scope.areas(request.areas)
    try:
        return SearchCriteria.create(scope, request.range, request.filter)
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def build_criteria(args: list[str], now: datetime) -> SearchCriteria:
    return criteria_from_request(parse_program_request(args, now))


def restored_request(args: list[str], now: datetime, store: SearchStore | None):
    saved = args[0] == "--saved"
    if saved and len(args) < 2:
        raise RuntimeError(PROGRAMS_USAGE)
    tail = args[2:] if saved else args[1:]
    as_json = classifications = False
    for arg in tail:
        if arg == "--json" and not as_json:
            as_json = True
        elif arg == "--classifications" and not classifications:
            classifications = True
        else:
            raise RuntimeError(PROGRAMS_USAGE)
    if saved:
        if store is None:
            raise RuntimeError("検索条件保存先を解決できません")
        library = store.load()
        if args[1] not in library.named:
            raise SearchError("missing")
        criteria = library.named[args[1]]
        allow = True
    elif store is not None:
        try:
            library = store.load()
            criteria = (
                library.last.restore_startup(now) if library.last else SearchCriteria.initial(now)
            )
            allow = True
        except SearchError as error:
            stderr_ln(f"検索条件を復元できません: {error}（当日・全国・指定なしで検索）")
            criteria = SearchCriteria.initial(now)
            allow = False
    else:
        criteria = SearchCriteria.initial(now)
        allow = True
    return _from_criteria(criteria, as_json, classifications), allow


def _from_criteria(
    criteria: SearchCriteria, as_json: bool, classifications: bool
) -> ProgramRequest:
    if criteria.scope.kind == ScopeKind.STATION:
        station, areas = criteria.scope.value, []
    elif criteria.scope.kind == ScopeKind.AREAS:
        station, areas = None, list(criteria.scope.value or [])
    else:
        station, areas = None, [f"JP{number}" for number in range(1, 48)]
    return ProgramRequest(station, areas, criteria.range, as_json, criteria.filter, classifications)


def _print_station_day(request: ProgramRequest, now: datetime) -> None:
    client = CatalogClient(catalog_fetch)
    listing = client.programs(request.station, request.range.start)
    if request.json:
        stdout_ln(
            json.dumps(
                _snapshot(listing, now, request.filter),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_list(listing, now, request.filter)
        if request.classifications:
            _print_classes(ObservedClassifications.collect(listing.programs))
    if listing.invalid_count:
        raise RuntimeError("取得結果に不正な放送枠があり、結果が不完全です")


def _acquire(request: ProgramRequest, now: datetime) -> None:
    import threading

    client = CatalogClient(catalog_fetch)
    cancel = threading.Event()

    def progress(item) -> None:
        day = "" if item.day is None else str(item.day)
        stderr_ln(
            f"{item.phase} {item.completed}/{item.total} / 失敗: {item.failed} / {item.target} {day}"
        )

    if request.station:
        report = acquire_stations(client, [request.station], request.range, cancel, progress)
    else:
        report = acquire_range(client, request.areas, request.range, cancel, progress)
    if request.json:
        stdout_ln(
            json.dumps(
                _report_snapshot(report, request, now), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        for listing in report.lists:
            _print_list(listing, now, request.filter)
        state = "取得完了" if report.complete() else "部分取得・未取得あり"
        stdout_ln(
            f"{state} / 放送日 {request.range.start}〜{request.range.end} / 局一覧成功: "
            f"{len(report.areas) - len(report.area_failures)}/{len(report.areas)}地域 / 番組表成功: "
            f"{len(report.lists)}/{len(report.stations) * len(request.range.days)}局・日"
        )
        for failure in report.area_failures:
            stderr_ln(f"未取得地域 {failure.target}（対象局も未確定）: {failure.error}")
        for failure in report.schedule_failures:
            day = "" if failure.day is None else str(failure.day)
            stderr_ln(f"未取得番組表 {failure.target} {day}: {failure.error}")
        if request.classifications:
            _print_classes(
                ObservedClassifications.collect(
                    [program for listing in report.lists for program in listing.programs]
                )
            )
    if not report.complete():
        raise RuntimeError("取得結果に欠落があります。同じコマンドの再実行で全件を再取得できます")


def _snapshot(listing: ProgramList, now: datetime, criteria_filter: Filter) -> dict:
    programs = []
    for program in listing.programs:
        if not criteria_filter.matches(program):
            continue
        status = broadcast_status(program.key.starts_at, program.ends_at, now)
        programs.append(
            {
                "description": program.description,
                "ends_at": rfc3339(program.ends_at),
                "info": program.info,
                "listening_availability": "unknown",
                "performer": program.performer,
                "personality_genres": [
                    {"id": item.id, "name": item.name} for item in program.personality_genres
                ],
                "program_genres": [
                    {"id": item.id, "name": item.name} for item in program.program_genres
                ],
                "program_id": program.program_id,
                "starts_at": rfc3339(program.key.starts_at),
                "station_id": program.key.station_id,
                "status": status,
                "status_label": STATUS_LABEL[status],
                "tags": program.tags,
                "title": program.title,
            }
        )
    observed = ObservedClassifications.collect(listing.programs)
    return {
        "as_of": rfc3339(now),
        "broadcast_day": listing.broadcast_day.isoformat(),
        "complete": listing.invalid_count == 0,
        "duplicate_count": listing.duplicate_count,
        "filters": criteria_filter.to_dict(),
        "invalid_count": listing.invalid_count,
        "matched_program_count": len(programs),
        "observed_classifications": _observed(observed),
        "programs": programs,
        "source_program_count": len(listing.programs),
        "station_id": listing.station_id,
        "station_name": listing.station_name,
        "timezone": "Asia/Tokyo",
    }


def _observed(observed: ObservedClassifications) -> dict:
    def classes(items):
        return [{"id": item.id, "name": item.name} for item in items]

    return {
        "personality_genres": classes(observed.personality_genres),
        "program_genres": classes(observed.program_genres),
        "scope": "observed_in_current_acquisition",
        "unclassified_personalities": observed.unclassified_personalities,
        "unclassified_programs": observed.unclassified_programs,
    }


def _report_snapshot(report, request: ProgramRequest, now: datetime) -> dict:
    schedules = [_snapshot(item, now, request.filter) for item in report.lists]
    return {
        "area_failures": [
            {"broadcast_day": None, "reason": str(item.error), "target": item.target}
            for item in report.area_failures
        ],
        "areas": report.areas,
        "as_of": rfc3339(now),
        "broadcast_day": request.range.start.isoformat() if len(request.range.days) == 1 else None,
        "complete": report.complete(),
        "date_range": {
            "days": len(request.range.days),
            "end": request.range.end.isoformat(),
            "start": request.range.start.isoformat(),
        },
        "filters": request.filter.to_dict(),
        "matched_program_count": sum(item["matched_program_count"] for item in schedules),
        "observed_classifications": _observed(
            ObservedClassifications.collect(
                [program for item in report.lists for program in item.programs]
            )
        ),
        "requested_schedules": len(report.stations) * len(request.range.days),
        "schedule_failures": [
            {
                "broadcast_day": None if item.day is None else item.day.isoformat(),
                "reason": str(item.error),
                "target": item.target,
            }
            for item in report.schedule_failures
        ],
        "schedules": schedules,
        "source_program_count": sum(len(item.programs) for item in report.lists),
        "stations": [
            {"areas": item.areas, "id": item.station.id, "name": item.station.name}
            for item in report.stations
        ],
        "successful_schedules": len(report.lists),
        "successful_stations": len({item.station_id for item in report.lists}),
        "timezone": "Asia/Tokyo",
    }


def _print_list(listing: ProgramList, now: datetime, criteria_filter: Filter) -> None:
    value = _snapshot(listing, now, criteria_filter)
    stdout_ln(
        f"{listing.station_name} ({listing.station_id}) / 放送日 {listing.broadcast_day} / 日本時間（実行時点の状態）"
    )
    for item in value["programs"]:
        stdout_ln(f"{item['starts_at']}–{item['ends_at']} [{item['status_label']}] {item['title']}")
        stdout_ln(
            f"  出演: {item['performer']} / 番組ジャンル: {_labels(item['program_genres'])} / "
            f"出演者分類: {_labels(item['personality_genres'])}"
        )
    stdout_ln(
        f"一致{value['matched_program_count']}件 / 取得{len(listing.programs)}件 / "
        f"不正・日付不一致: {listing.invalid_count}件 / 重複: {listing.duplicate_count}件 / 全件: 再生可否未確認"
    )


def _labels(items: list[dict]) -> str:
    names = [item["name"] or item["id"] for item in items]
    names = [name for name in names if name]
    return "未分類" if not names else "、".join(names)


def _print_classes(observed: ObservedClassifications) -> None:
    stdout_ln("今回の取得範囲で観測した分類（全分類を保証しません）")
    for label, items in (
        ("番組ジャンル", observed.program_genres),
        ("出演者分類", observed.personality_genres),
    ):
        for item in items:
            stdout_ln(f"{label}: {item.id} / {item.name}")
    stdout_ln(
        f"未分類: 番組ジャンル{observed.unclassified_programs}枠 / 出演者分類{observed.unclassified_personalities}枠"
    )


def chapters(args: list[str]) -> None:
    if args == ["--help"]:
        stdout_ln(CHAPTERS_USAGE)
        return
    if len(args) != 2:
        raise RuntimeError(CHAPTERS_USAGE)
    validate_station(args[0])
    cursor = parse_time(args[1])
    from radiko.prepare import ChapterFailure, inspect_remote_chapters

    try:
        view = inspect_remote_chapters(args[0], cursor)
    except ChapterFailure as error:
        raise RuntimeError(str(error)) from error
    from radiko.domain import JST

    start = view.starts_at.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    end = view.ends_at.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    stdout_ln(f"{view.title or '番組名未取得'} / {view.station} / {start}–{end}（日本時間）")
    if view.kind == "unresolved":
        stdout_ln(f"{view.issue}（取得・位置補正を省略）")
        return
    if view.kind == "unavailable":
        stdout_ln("チャプター未提供（生成待ちの場合があります）")
        return
    if view.kind == "aligned":
        stdout_ln("放送枠・イベント原点一致（このCLIは閲覧のみ）")
    else:
        stdout_ln("メタデータ一致・音声位置未検証（閲覧のみ）")
    stdout_ln(f"{view.count}件 / 楽曲除外{view.music}件 / 不正位置等{view.invalid}件")
    for index, (begin, finish, title) in enumerate(view.rows or [], start=1):
        stdout_ln(f"{index}: {begin:.3f}–{finish:.3f}秒 {title}")


def play(args: list[str], data_dir: Path | None) -> None:
    if len(args) == 2 and args[1] == "--help":
        stdout_ln(PLAY_USAGE)
        return
    now = datetime.now(timezone.utc)
    mode, station, cursor, seconds, offset, interactive = _play_request(args, now)
    session = _require_session(data_dir)
    import threading

    commands = None
    notice = None
    stop = threading.Event()
    previous = _sigint_stop(stop)
    if interactive and sys.stdin.isatty():
        import queue

        box: queue.Queue[str] = queue.Queue(4)

        def reader() -> None:
            for line in sys.stdin:
                if stop.is_set():
                    return
                while not stop.is_set():
                    try:
                        box.put(line, timeout=0.1)
                        break
                    except queue.Full:
                        continue

        threading.Thread(target=reader, daemon=True).start()
        stderr_ln(
            "操作: live 局ID / timefree 局ID YYYYMMDDHHmmss [--offset 秒数] / status / pause / "
            "resume / volume 0〜100 / seek 秒数 / +15 / -15 / q（Enterで確定）"
        )

        def commands() -> str | None:
            try:
                return box.get(timeout=0.05)
            except queue.Empty:
                return None

        notice = stderr_ln
    try:
        membership = _membership(session, stop)
    except RuntimeError:
        if stop.is_set():
            _restore_sigint(previous)
            stdout_ln("中断しました")
            return
        raise
    if stop.is_set():
        _restore_sigint(previous)
        stdout_ln("中断しました")
        return
    stderr_ln("放送枠・配信条件を確認して連続再生を準備しています…")
    from radiko.domain import JST
    from radiko.prepare import play_station

    def ready(program) -> None:
        start = program.start.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
        end = program.end.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
        label = "Live" if mode == "live" else "TimeFree"
        stdout_ln(
            f"{program.title or '番組名未取得'} / {station} / {label} / {start}–{end}（日本時間） / Ctrl+Cで停止"
        )

    try:
        report = play_station(
            station,
            mode,
            cursor,
            now,
            session,
            membership,
            offset,
            None if seconds is None else float(seconds),
            stop.is_set,
            commands,
            on_notice=notice,
            on_ready=ready,
        )
    except ProbeError as error:
        _restore_sigint(previous)
        if stop.is_set() or error.code == "cancelled":
            stdout_ln("中断しました")
            return
        raise
    except Exception:
        _restore_sigint(previous)
        raise
    else:
        _restore_sigint(previous)
    reason = {
        "program-end": "番組終了",
        "limit": "指定時間で停止しました",
        "cancelled": "中断しました",
    }[report.reason]
    stdout_ln(reason)
    if report.reason != "cancelled":
        stdout_ln(
            f"出力: {report.frames}フレーム / {report.sample_rate}Hz / {report.channels}ch / "
            f"{report.segments}セグメント / バッファ不足: {report.underrun_frames}フレーム"
        )


def _play_request(args: list[str], now: datetime):
    positional = []
    seconds = offset = None
    interactive = False
    index = 1
    while index < len(args):
        arg = args[index]
        if arg == "--seconds" and seconds is None:
            try:
                seconds = int(_value(args, index, PLAY_USAGE))
            except ValueError as error:
                raise RuntimeError(PLAY_USAGE) from error
            if not 1 <= seconds <= 86400:
                raise RuntimeError(PLAY_USAGE)
            index += 1
        elif arg == "--offset" and offset is None:
            offset = _position(_value(args, index, PLAY_USAGE))
            index += 1
        elif arg == "--interactive" and not interactive:
            interactive = True
        elif arg.startswith("--"):
            raise RuntimeError(PLAY_USAGE)
        else:
            positional.append(arg)
        index += 1
    if args[0] == "play-live" and len(positional) == 1:
        mode = "live"
    elif args[0] == "play-timefree" and len(positional) == 2:
        mode = "timefree"
    else:
        raise RuntimeError(PLAY_USAGE)
    if mode == "live" and offset is not None:
        raise RuntimeError("--offsetはタイムフリーで使用してください")
    validate_station(positional[0])
    if mode == "live":
        cursor = now.astimezone()
    else:
        cursor = parse_time(positional[1])
    if mode == "timefree" and cursor > now.astimezone():
        raise ProbeError("upcoming")
    return mode, positional[0], cursor, seconds, offset or 0, interactive


def _position(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise RuntimeError("秒数を指定してください") from error
    if seconds != seconds or not 0 <= seconds <= 86400:
        raise RuntimeError("位置は0〜86400秒で指定してください")
    return seconds


def probe_media(args: list[str], data_dir: Path | None) -> None:
    validate_station(args[1])
    now = datetime.now(timezone.utc)
    if args[0] == "probe-live" or (args[0] == "play-sample" and len(args) == 2):
        mode = "live"
        cursor = now
    else:
        mode = "timefree"
        cursor = parse_time(args[2])
        if cursor > now:
            raise ProbeError("upcoming")
    session = _require_session(data_dir)
    import threading

    stop = threading.Event()
    membership = _membership(session, stop)
    stderr_ln("会員セッション有効。放送枠・配信認証・HLSを検証しています…")
    from radiko.prepare import probe

    report = probe(
        args[1], mode, cursor, now, session, membership, args[0] == "play-sample", stop.is_set
    )
    _print_probe(report)
    if args[0] == "play-sample":
        if not report.samples:
            raise RuntimeError("取得したサンプルは音声出力に対応していません")
        from radiko.output import play_samples

        summary = play_samples(report.samples, report.sample_rate, report.channels)
        stdout_ln(
            f"先頭1セグメントの出力完了: {summary.frames}フレーム / {summary.sample_rate}Hz / "
            f"{summary.channels}ch / バッファ不足: {summary.underrun_frames}フレーム"
        )
        stdout_ln(
            "サンプル末尾で停止しました。連続再生にはplay-live／play-timefreeを使用してください"
        )
    else:
        stdout_ln("音声出力は行っていません。30分連続再生・実際の番組末尾の停止は未検証です")


def _require_session(data_dir: Path | None):
    saved = _session_store(data_dir).load()
    if saved is None:
        raise RuntimeError("未ログインです。loginでログインしてください")
    return saved


def _membership(session, stop):
    from radiko.auth import AuthError, MemberClient

    try:
        return MemberClient.create(stop.is_set).check(session)
    except AuthError as error:
        if error.code == "cancelled":
            raise RuntimeError("中断しました") from error
        raise


def _print_probe(report) -> None:
    from radiko.domain import JST

    start = report.starts_at.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    end = report.ends_at.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    stdout_ln(f"{report.station} / {report.mode} / {start}–{end}（日本時間）")
    outside = "true" if report.outside_area else "false"
    end_list = "true" if report.end_list else "false"
    stdout_ln(
        f"認証エリア: {report.area} / エリア外: {outside} / 失敗した配信先: {report.failed_endpoints}"
    )
    stdout_ln(
        f"HLS取得成功: {report.segments}セグメント / {report.playlist_seconds:.3f}秒 / ENDLIST: {end_list}"
    )
    codec = report.declared_codec or "未宣言"
    stdout_ln(
        f"HLS宣言コーデック: {codec} / サンプル形式: {report.container} / {report.sample_bytes}バイト"
    )
    if report.decode_error and report.packets == 0:
        if report.decode_error.startswith("HE-AAC"):
            stdout_ln(report.decode_error)
        else:
            stdout_ln(f"サンプル取得済み。AAC検証失敗: {report.decode_error}")
        return
    stdout_ln(
        "AACセグメントのPyAVデコード成功: "
        f"{report.packets}パケット / {report.frames}フレーム / {report.sample_rate}Hz / {report.channels}ch"
    )
    stdout_ln(
        "AACコア: "
        f"{report.core_sample_rate}Hz / {report.core_channels}ch / SBR検出: {str(report.sbr_detected).lower()} / "
        f"モノラルからステレオへの拡張: {str(report.stereo_expansion).lower()}"
    )
    stdout_ln(
        "デコードしたメディア: "
        f"{report.media_duration:.3f}秒 / 処理時間: {report.decode_duration:.3f}秒 / 非ゼロサンプル: {report.nonzero_samples}"
    )
    stdout_ln("ADTSの公開デコードAPIにビットストリームのPSフラグはなく、明示シグナルは未確認です")


def command_line(line: str, now: datetime):
    """対話再生の1行。`--seconds` と `--interactive` は切替先に付けない。"""
    text = line.strip()
    if text in {"status", "pause", "resume", "q", "stop"}:
        return text
    if text in {"+15", "-15"}:
        return text
    if text.startswith("volume "):
        try:
            volume = int(text[7:].strip())
        except ValueError as error:
            raise RuntimeError("音量は0〜100で指定してください") from error
        if not 0 <= volume <= 100:
            raise RuntimeError("音量は0〜100で指定してください")
        return ("volume", volume)
    if text.startswith("seek "):
        return ("seek", _position(text[5:]))
    if text.startswith("live ") or text.startswith("timefree "):
        parts = text.split()
        mapped = [f"play-{parts[0]}", *parts[1:]]
        mode, station, cursor, seconds, offset, interactive = _play_request(mapped, now)
        if seconds is not None or interactive:
            raise RuntimeError("切替先に--secondsと--interactiveは指定できません")
        return ("select", mode, station, cursor, offset)
    raise RuntimeError(
        "live 局ID / timefree 局ID YYYYMMDDHHmmss [--offset 秒数] / status / pause / resume / "
        "volume 0〜100 / seek 秒数 / +15 / -15 / q を入力してください"
    )
