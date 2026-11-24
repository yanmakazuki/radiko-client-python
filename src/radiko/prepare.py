"""配信の準備。URL は本番の形で作り、取得は stream_get に任せる。"""

from __future__ import annotations

import json
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin

import numpy as np

from radiko import auth, output
from radiko.catalog import CatalogError, parse_endpoints, parse_stations
from radiko.chapters import MAX_CHAPTER_BYTES, ChapterError, chapters_from_json
from radiko.domain import JST, ProgramKey, broadcast_day
from radiko.fragmented import FragmentedAacDecoder
from radiko.hls import (
    HlsCursor,
    parse_attribute,
    parse_media_playlist,
    segment_discontinuity,
    segment_schedule,
)
from radiko.http_client import stream_get, transport_overrides
from radiko.interactive import InteractiveControl
from radiko.media import AacError, AdtsDecoder, id3_offset
from radiko.reauth import ReauthState
from radiko.storage import MemberSession, Membership
from radiko.stream import (
    ProbeError,
    SlotProgram,
    area_contract,
    availability,
    eligible,
)
from radiko.timing import ReconnectBudget, accept_fragment, segment_wall, trim_pcm
from radiko.urls import append_query

API = "https://api.radiko.jp"
META = "https://api.annex-cf.radiko.jp/v1/programs/list"
TEXT_LIMIT = 1024 * 1024
SAMPLE_LIMIT = 8 * 1024 * 1024


@dataclass
class ResolvedProgram:
    """API から解決した1番組。放送時刻・タイムフリー可否・イベント情報を持つ。

    `slot()` でカタログ層の SlotProgram に変換する。
    """

    title: str | None
    start: datetime
    end: datetime
    timefree: tuple[bool | None, bool | None, bool | None, bool | None]
    failed_record: bool | None
    event_id: str | None
    origin: datetime | None
    station_id: str

    def slot(self) -> SlotProgram:
        """カタログ層の SlotProgram に変換する。"""
        return SlotProgram(self.start, self.end, self.timefree, self.failed_record)


@dataclass
class ProbeReport:
    """probe の結果。番組メタデータ・コンテナ・サンプル復号の統計を持つ。

    `__repr__` はサンプルの配列を伏せる。`samples` は retain=True のときだけ入る。
    """

    station: str
    starts_at: datetime
    ends_at: datetime
    mode: str
    area: str
    outside_area: bool
    failed_endpoints: int
    segments: int
    playlist_seconds: float
    end_list: bool
    declared_codec: str | None
    container: str
    sample_bytes: int
    packets: int
    frames: int
    sample_rate: int
    channels: int
    core_sample_rate: int
    core_channels: int
    sbr_detected: bool
    stereo_expansion: bool
    media_duration: float
    decode_duration: float
    nonzero_samples: int
    decode_error: str | None
    samples: np.ndarray | None
    title: str | None

    def __repr__(self) -> str:
        """局とモードだけを出し、サンプル配列は REDACTED にする。"""
        return f"ProbeReport(station={self.station!r}, mode={self.mode!r}, samples=[REDACTED])"


@dataclass
class PlayReport:
    """再生ループの結果。

    `reason` は `limit`（指定秒で停止）/ `cancelled`（停止・キャンセル）/
    `program-end`（番組終端）。`frames` は出力フレーム数、`segments` は再生した
    セグメント数、`underrun_frames` はアンダーラン、`sample_rate` / `channels` は
    最後に使った出力、`messages` は操作中の通知。`title` / `station` / `mode` /
    `starts_at` / `ends_at` は番組のメタデータ。
    """

    reason: str
    frames: int
    sample_rate: int
    channels: int
    segments: int
    underrun_frames: int
    title: str | None
    station: str
    mode: str
    starts_at: datetime
    ends_at: datetime
    messages: list[str] | None = None


class ChapterFailure(Exception):
    """チャプター取得の失敗。メッセージはそのまま利用者向けに表示できる。"""

    def __init__(self, text: str) -> None:
        """利用者向けメッセージをそのまま例外メッセージにする。"""
        super().__init__(text)


@dataclass
class ChapterView:
    """チャプター取得の表示用ビュー。

    `kind` は `aligned`（番組と対応済み）/ `unresolved`（対応付け未確認）/
    `unavailable`（提供なし）。`rows` は (開始秒, 終了秒, 見出し)、`count` /
    `music` / `invalid` は件数の内訳。
    """

    station: str
    title: str | None
    starts_at: datetime
    ends_at: datetime
    kind: str
    issue: str | None = None
    count: int = 0
    music: int = 0
    invalid: int = 0
    rows: list[tuple[float, float, str]] | None = None
    summaries: list[str] = field(default_factory=list)


def fetch_program(station: str, cursor: datetime, cancelled) -> ResolvedProgram:
    """指定時刻を含む放送日の番組を1件取得する。

    放送日は JST 05:00 境界。`ProbeError("program" / "not-found")` を送出しうる。
    """
    day = broadcast_day(cursor).strftime("%Y%m%d")
    after_utc = (cursor + timedelta(seconds=1)).astimezone(timezone.utc)
    after_text = after_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urlencode(
        [
            ("broadcastDate", day),
            ("lastStartAt", after_text),
            ("limit", "1"),
            ("stationId", station),
        ]
    )
    reply = stream_get(f"{META}?{query}", limit=TEXT_LIMIT, cancelled=cancelled, redirects=False)
    return parse_program(reply.body, station, cursor)


def _rfc3339_secs(moment: datetime) -> str:
    """秒精度の RFC3339（...Z）にする。タイムゾーンは変換しない。"""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_program(data: bytes, station: str, cursor: datetime) -> ResolvedProgram:
    """番組 JSON を検証して ResolvedProgram にする。

    1件だけ・cursor が放送枠内・stationId が一致または欠落、を要求する。blackout の
    tsInNg / tsplusInNg / tsOutNg / tsplusOutNg は 0 を可、2 を不可、他は未確認。不正は
    `ProbeError("program")`、該当なしは `ProbeError("not-found")`。
    """
    try:
        root = json.loads(data.decode())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProbeError("program") from error
    _Probe = ProbeError

    programs = root.get("programs") if isinstance(root, dict) else None
    if not isinstance(programs, list):
        raise _Probe("program")
    if not programs:
        raise _Probe("not-found")
    if len(programs) != 1 or not isinstance(programs[0], dict):
        raise _Probe("program")
    value = programs[0]
    if value.get("stationId") not in {None, station}:
        raise _Probe("program")
    start = _time_field(value, "startAt")
    end = _time_field(value, "endAt")
    if end <= start or not (start <= cursor < end):
        raise _Probe("program")
    conditions = value.get("blackout") if isinstance(value.get("blackout"), dict) else None

    def read_flag(key: str) -> int | None:
        """blackout の条件値を整数で返す。真偽値・欠落・数値でない文字列は None。"""
        if not conditions or key not in conditions:
            return None
        raw = conditions[key]
        if isinstance(raw, bool) or raw is None:
            return None
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            try:
                return int(raw)
            except ValueError:
                return None
        return None

    def allowed(key: str) -> bool | None:
        """blackout の 0 を可、2 を不可、それ以外を未確認（None）にする。"""
        flag = read_flag(key)
        if flag == 0:
            return True
        if flag == 2:
            return False
        return None

    failed = read_flag("failedRecord")
    event_id = value.get("eventId")
    origin_text = value.get("episodeIntroductionStartAt")
    origin = None
    if (
        isinstance(event_id, str)
        and event_id
        and len(event_id) <= 128
        and all(char.isascii() and (char.isalnum() or char in "-_") for char in event_id)
        and value.get("stationId") == station
        and isinstance(origin_text, str)
    ):
        try:
            origin = datetime.fromisoformat(origin_text.replace("Z", "+00:00")).astimezone(JST)
        except ValueError:
            event_id = None
            origin = None
    else:
        event_id = None
    title = value.get("title")
    if isinstance(title, str):
        title = "".join(char for char in title if unicodedata.category(char) != "Cc")[:512]
        title = title if title.strip() else None
    else:
        title = None
    return ResolvedProgram(
        title,
        start.astimezone(JST),
        end.astimezone(JST),
        (allowed("tsInNg"), allowed("tsplusInNg"), allowed("tsOutNg"), allowed("tsplusOutNg")),
        None if failed is None else failed != 0,
        event_id if isinstance(event_id, str) else None,
        origin,
        station,
    )


def _time_field(value: dict, key: str) -> datetime:
    """ISO 8601 の時刻フィールドを datetime にする。文字列でない・不正・TZ なしは program。"""
    text = value.get(key)
    if not isinstance(text, str):
        raise ProbeError("program")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProbeError("program") from error
    if parsed.tzinfo is None:
        raise ProbeError("program")
    return parsed


def authenticate_stream(session: MemberSession, cancelled) -> tuple[dict[str, str], str]:
    """stream_get を fetch に載せるだけ。手順は radiko.auth.authenticate に一つだけ。"""

    def fetch(path: str, headers: dict[str, str]):
        """認証 API を stream_get で叩き、応答を返す。64KB 上限とキャンセルを渡す。"""
        return stream_get(f"{API}{path}", headers, limit=64 * 1024, cancelled=cancelled)

    return auth.authenticate(fetch, session)


def prepare(
    station: str,
    mode: str,
    cursor: datetime,
    now: datetime,
    session: MemberSession,
    membership: Membership,
    position: float = 0.0,
    cancelled=None,
):
    """再生に必要な番組・認証・エリア・配信先 URL を解決する。

    戻り値は (program, 認証ヘッダ, エリア, エリア外か, プレイリスト URL の候補)。
    各取得の失敗は `ProbeError` に stage（放送枠取得 / 配信認証 / 地域の局一覧取得 /
    配信先設定の取得）を付けて送出する。position を検証し、mode は timefree 以外を
    live として扱う。
    """
    cancelled = cancelled or (lambda: False)
    try:
        program = fetch_program(station, cursor, cancelled)
    except ProbeError as error:
        raise error.at("放送枠取得") from error
    timefree = mode == "timefree"
    try:
        eligible(program.slot(), timefree, now, membership)
    except ProbeError:
        raise
    duration = (program.end - program.start).total_seconds()
    if (mode == "live" and position) or position >= duration or position < 0:
        raise ProbeError("position")
    try:
        auth_headers, area = authenticate_stream(session, cancelled)
    except ProbeError as error:
        raise error.at("配信認証") from error
    try:
        outside = not _local_station(area, station, cancelled)
    except ProbeError as error:
        raise error.at("地域の局一覧取得") from error
    contract = area_contract(outside, membership.area_free)
    if contract is not None:
        raise contract
    if timefree:
        availability(program.slot(), now, outside)
    try:
        config = stream_get(
            f"https://radiko.jp/v3/station/stream/pc_html5/{station}.xml",
            limit=TEXT_LIMIT,
            cancelled=cancelled,
        )
        targets = parse_endpoints(config.body, timefree, outside)
    except ProbeError as error:
        raise error.at("配信先設定の取得") from error
    except ValueError as error:
        raise ProbeError("xml").at("配信先設定の取得") from error
    urls = []
    for target in targets:
        urls.append(_playlist_url(target, station, mode, program, outside, position))
    return program, auth_headers, area, outside, urls


def _local_station(area: str, station: str, cancelled) -> bool:
    """エリアの局一覧を取得し、station が含まれるか返す。XML 不正は xml。"""
    reply = stream_get(
        f"https://radiko.jp/v3/station/list/{area}.xml",
        limit=TEXT_LIMIT,
        cancelled=cancelled,
    )
    try:
        stations = parse_stations(reply.body, area)
    except CatalogError as error:
        raise ProbeError("xml") from error
    return any(item.id == station for item in stations)


def _playlist_url(
    target: str, station: str, mode: str, program: ResolvedProgram, outside: bool, position: float
) -> str:
    """配信先 URL に station_id・lsid・l（timefree=60 / live=15）・type（域外=c / 域内=b）を付け、
    timefree は番組時刻、seek は10秒手前を足す。
    """
    lsid = secrets.token_hex(16)
    pairs = [
        ("station_id", station),
        ("lsid", lsid),
        ("l", "60" if mode == "timefree" else "15"),
        ("type", "c" if outside else "b"),
    ]
    if mode == "timefree":
        start = program.start.astimezone(JST).strftime("%Y%m%d%H%M%S")
        end = program.end.astimezone(JST).strftime("%Y%m%d%H%M%S")
        pairs.extend([("start_at", start), ("ft", start), ("end_at", end), ("to", end)])
    url = append_query(target, pairs)
    if position:
        seek_at = program.start + timedelta(seconds=max(position - 10, 0))
        url = append_query(url, [("seek", seek_at.astimezone(JST).strftime("%Y%m%d%H%M%S"))])
    return url


def inspect_hls(url: str, headers: dict[str, str], retain: bool, cancelled) -> ProbeReport:
    """マスター/メディアプレイリストをたどり、先頭セグメントを probe する。

    マスター/メディアの取得は最大4回。帯域が最小のバリアントを選び、暗号化の宣言を
    引き継ぐ。メディアプレイリストが得られない場合は `ProbeError("playlist")`。
    """
    codec = None
    encrypted = False
    current = url
    for _ in range(4):
        reply = stream_get(
            current,
            headers,
            limit=TEXT_LIMIT,
            cancelled=cancelled,
            redirects=True,
            readonly_retry=True,
        )
        text = reply.body.decode()
        current = reply.url
        if "#EXT-X-STREAM-INF:" in text and "#EXTINF:" not in text:
            encrypted |= _session_encrypted(text)
            codec, nxt = _master_target(text, current)
            current = nxt
            continue
        media = parse_media_playlist(text)
        return _sample(media, current, headers, codec, retain, cancelled, encrypted)
    raise ProbeError("playlist")


def _session_encrypted(text: str) -> bool:
    """マスターの SESSION-KEY に NONE 以外の METHOD があれば True。"""
    for line in text.splitlines():
        if line.startswith("#EXT-X-SESSION-KEY:"):
            method = parse_attribute(line, "METHOD")
            if method not in {None, "NONE"}:
                return True
    return False


def _master_target(text: str, base: str) -> tuple[str | None, str]:
    """マスターから I-FRAMES-ONLY を除いた最小 BANDWIDTH の variant を選び、(CODECS, 絶対 URI) を返す。
    AUDIO グループがあればその URI を使う。
    """
    variants = []
    pending = None
    pending_i_frames = False
    i_frames_only = False
    alternatives = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-I-FRAMES-ONLY"):
            i_frames_only = True
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = line
            pending_i_frames = i_frames_only
            i_frames_only = False
        elif pending and line and not line.startswith("#"):
            if not pending_i_frames:
                variants.append((pending, line))
            pending = None
        elif line.startswith("#EXT-X-MEDIA:"):
            alternatives.append(line)
    if not variants:
        raise ProbeError("playlist")
    chosen_attr, chosen_uri = min(variants, key=lambda item: _bandwidth(item[0]))
    codec = _safe_codec(parse_attribute(chosen_attr, "CODECS"))
    group = parse_attribute(chosen_attr, "AUDIO")
    uri = chosen_uri
    if group:
        for item in alternatives:
            if (
                parse_attribute(item, "TYPE") == "AUDIO"
                and parse_attribute(item, "GROUP-ID") == group
                and parse_attribute(item, "URI")
            ):
                uri = parse_attribute(item, "URI") or uri
                break
    return codec, _join(base, uri)


def _safe_codec(text: str | None) -> str | None:
    """CODECS を表示・比較用に制限する。100文字超や ASCII 英数と .,-_ 以外なら None。"""
    if text is None:
        return None
    return (
        text
        if len(text) <= 100
        and all(char.isascii() and (char.isalnum() or char in ".,-_ ") for char in text)
        else None
    )


def _bandwidth(line: str) -> int:
    """BANDWIDTH を整数で返す。無い・不正なら 0（最小選択では最優先）。"""
    raw = parse_attribute(line, "BANDWIDTH") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


def _join(base: str, uri: str) -> str:
    """base を基準に uri を絶対 URL にする。"""
    return urljoin(base, uri)


def _fetch_sample_blob(media, playlist_url: str, headers, cancelled) -> tuple[bytes, bytes]:
    """先頭セグメントと、あれば初期化セグメントを取得する。"""
    first = media.segments[0]
    sample = b""
    if first.map_uri:
        sample = stream_get(
            _join(playlist_url, first.map_uri),
            {},
            limit=TEXT_LIMIT,
            cancelled=cancelled,
            redirects=True,
            readonly_retry=True,
        ).body
        if not sample:
            raise ProbeError("playlist")
    segment = stream_get(
        _join(playlist_url, first.uri),
        {},
        limit=SAMPLE_LIMIT,
        cancelled=cancelled,
        redirects=True,
        readonly_retry=True,
    ).body
    if not segment:
        raise ProbeError("playlist")
    return sample, segment


@dataclass
class _DecodedSample:
    """probe 用サンプルの復号結果。ProbeReport の対応するフィールドへ渡す。"""

    container: str
    packets: int
    frames: int
    rate: int
    channels: int
    core_rate: int
    core_channels: int
    sbr: bool
    stereo: bool
    media_duration: float
    decode_duration: float
    nonzero: int
    decode_error: str | None
    samples: np.ndarray | None


def _decode_sample(
    blob: bytes, sample: bytes, container: str, codec, retain: bool, cancelled
) -> _DecodedSample:
    """サンプルをコンテナ判定して復号し、ProbeReport 用の値を返す。"""
    packets = frames = rate = channels = core_rate = core_channels = 0
    sbr = stereo = False
    media_duration = decode_duration = 0.0
    nonzero = 0
    decode_error = None
    samples = None
    offset = id3_offset(blob)
    adts = (
        offset is not None
        and len(blob) - offset >= 2
        and blob[offset] == 0xFF
        and (blob[offset + 1] & 0xF0) == 0xF0
    )
    if container == "encrypted":
        decode_error = None
    elif container.startswith("ISO BMFF") and sample:
        started = time.perf_counter()
        try:
            core = FragmentedAacDecoder(sample)
            decoded = core.decode_pcm(blob[len(sample) :], cancelled, codec)
            packets = decoded.packets
            frames = decoded.frames
            rate = decoded.sample_rate
            channels = decoded.channels
            core_rate = core.sample_rate
            core_channels = core.channels
            sbr = rate > core_rate
            stereo = core_channels == 1 and channels == 2
            media_duration = frames / rate
            decode_duration = time.perf_counter() - started
            nonzero = int(np.count_nonzero(decoded.samples))
            samples = decoded.samples if retain else None
        except AacError as error:
            decode_error = str(error)
    elif adts:
        try:
            pcm = AdtsDecoder().decode_segment_pcm(blob, codec, cancelled or (lambda: False))
            packets = pcm.summary.packets
            frames = pcm.summary.frames
            rate = pcm.summary.sample_rate
            channels = pcm.summary.channels
            core_rate = pcm.summary.core_sample_rate
            core_channels = pcm.summary.core_channels
            sbr = pcm.summary.sbr_detected
            stereo = pcm.summary.stereo_expansion
            media_duration = pcm.summary.media_duration
            decode_duration = pcm.summary.decode_duration
            nonzero = pcm.summary.nonzero_samples
            samples = pcm.samples if retain else None
        except AacError as error:
            decode_error = str(error)
    elif codec and any(
        part.strip() in {"mp4a.40.5", "mp4a.40.29"} for part in (codec or "").split(",")
    ):
        decode_error = "HE-AAC宣言に対応するコンテナまたは設定情報を確認できません（AACコアだけの成功とは扱いません）"
    return _DecodedSample(
        container,
        packets,
        frames,
        rate,
        channels,
        core_rate,
        core_channels,
        sbr,
        stereo,
        media_duration,
        decode_duration,
        nonzero,
        decode_error,
        samples,
    )


def _sample(
    media, playlist_url: str, headers, codec, retain: bool, cancelled, encrypted: bool = False
) -> ProbeReport:
    """先頭セグメント（と init）を取得・復号し、番組メタデータ以外を埋めた ProbeReport を返す。"""
    first = media.segments[0]
    sample, segment = _fetch_sample_blob(media, playlist_url, headers, cancelled)
    blob = sample + segment
    container = "encrypted" if encrypted or first.encrypted else container_name(blob)
    decoded = _decode_sample(blob, sample, container, codec, retain, cancelled)
    return ProbeReport(
        station="",
        starts_at=datetime.now(JST),
        ends_at=datetime.now(JST),
        mode="",
        area="",
        outside_area=False,
        failed_endpoints=0,
        segments=len(media.segments),
        playlist_seconds=sum(item.duration for item in media.segments),
        end_list=media.end_list,
        declared_codec=codec,
        container=decoded.container,
        sample_bytes=len(blob),
        packets=decoded.packets,
        frames=decoded.frames,
        sample_rate=decoded.rate,
        channels=decoded.channels,
        core_sample_rate=decoded.core_rate,
        core_channels=decoded.core_channels,
        sbr_detected=decoded.sbr,
        stereo_expansion=decoded.stereo,
        media_duration=decoded.media_duration,
        decode_duration=decoded.decode_duration,
        nonzero_samples=decoded.nonzero,
        decode_error=decoded.decode_error,
        samples=decoded.samples,
        title=None,
    )


def container_name(data: bytes) -> str:
    """ID3 を除いた先頭バイトからコンテナ名を判定する。

    ISO BMFF / fragmented MP4、ADTS（MPEG-2 / MPEG-4 / MPEG-4 CRC）、MPEG-TS、
    ID3 + unknown payload、unknown を返す。ID3 ヘッダが不正なら "invalid ID3"。
    判定できなくても例外は出さない。
    """
    offset = id3_offset(data)
    if offset is None:
        return "invalid ID3"
    view = data[offset:]
    if len(view) >= 8 and view[4:8] in {b"ftyp", b"styp", b"moof", b"sidx"}:
        return "ISO BMFF / fragmented MP4"
    if len(view) >= 2 and view[0] == 0xFF and view[1] & 0xF6 == 0xF0:
        if view[1] & 8:
            return "MPEG-2 AAC ADTS"
        if view[1] & 1 == 0:
            return "MPEG-4 AAC ADTS (CRC)"
        return "MPEG-4 AAC ADTS"
    if len(view) > 188 and view[0] == 0x47 and view[188] == 0x47:
        return "MPEG-TS"
    if offset:
        return "ID3 + unknown payload"
    return "unknown"


def probe(
    station: str,
    mode: str,
    cursor: datetime,
    now: datetime,
    session: MemberSession,
    membership: Membership,
    retain: bool,
    cancelled=None,
) -> ProbeReport:
    """番組を解決して配信先ごとに HLS を試し、最初に成功した ProbeReport を返す。

    authentication / limited は即座に送出し、それ以外の失敗は次の配信先へ進む。
    全滅時は最後のエラーを `HLS・サンプル取得` の stage 付きで送出する。
    """
    program, headers, area, outside, urls = prepare(
        station, mode, cursor, now, session, membership, 0.0, cancelled
    )
    last = ProbeError("endpoint")
    for index, url in enumerate(urls):
        try:
            report = inspect_hls(url, headers, retain, cancelled)
        except ProbeError as error:
            if error.code in {"authentication", "limited"}:
                raise error
            last = error
            continue
        report.station = station
        report.starts_at = program.start
        report.ends_at = program.end
        report.mode = "Live" if mode == "live" else "TimeFree"
        report.area = area
        report.outside_area = outside
        report.failed_endpoints = index
        report.title = program.title
        return report
    raise last.at("HLS・サンプル取得")


def _apply_seek(control, media, sequence: int, media_seconds: float, origin: datetime, pending):
    """目標より前に終わるセグメントは飛ばし、着地セグメントは後で切り詰める。

    窓の外なら新しい開始位置で準備し直す（戻り値の最後が再準備する秒数）。
    """

    if not control.accepted or control.applied == len(control.accepted):
        return sequence, media_seconds, pending, None
    target = control.accepted[-1]
    control.applied = len(control.accepted)
    target_at = origin + timedelta(seconds=target)
    schedule = segment_schedule(media)
    first_wall: datetime | None = None
    last_wall: datetime | None = None
    last_duration = 0.0
    cursor = media.media_sequence
    skipping = True
    for segment, wall in zip(media.segments, schedule.walls):
        if wall is None:
            skipping = False
            continue
        if first_wall is None:
            first_wall = wall
        last_wall = wall
        last_duration = segment.duration
        if skipping and target_at >= wall + timedelta(seconds=segment.duration):
            cursor += 1
        else:
            skipping = False
    if (
        first_wall is not None
        and last_wall is not None
        and (target_at < first_wall or target_at >= last_wall + timedelta(seconds=last_duration))
    ):
        return sequence, media_seconds, pending, target
    return cursor, target, (target_at, target), None


def _segment_at(media, sequence: int):
    """通し番号のセグメントを返す。窓の外なら None。"""
    start = media.media_sequence
    if sequence < start or sequence >= start + len(media.segments):
        return None
    return media.segments[sequence - start]


def _segment_discontinuity(media, sequence: int):
    """通し番号の累積不連続番号を返す。窓の外なら None。"""
    return segment_discontinuity(media, sequence)


def _refresh_playlist(
    url,
    source,
    headers,
    cancelled,
    state,
    mode,
    played,
    stage,
):
    """一覧を取り直す。401 は再認証、ライブの失効は source から作り直す。"""

    def fetch(target, current_headers):
        """一覧を取得して解析し、(MediaPlaylist, 最終 URL) を返す。1MB 上限・リダイレクト・再試行付き。"""
        reply = stream_get(
            target,
            current_headers,
            limit=TEXT_LIMIT,
            cancelled=cancelled,
            redirects=True,
            readonly_retry=True,
        )
        return parse_media_playlist(reply.body.decode()), reply.url

    def open_source():
        """_open_media(source) を呼び、失敗は ProbeError として返す（送出しない）。"""
        try:
            return _open_media(source, headers, cancelled)
        except ProbeError as error:
            return error

    try:
        media, current = fetch(url, headers)
        return media, current, headers
    except ProbeError as error:
        # Rust の live_playlist_expired。ライブで再生済みのときだけ source へ逃げる。
        expired = (
            mode == "live"
            and played
            and error.code
            in {
                "not-found",
                "authentication",
                "denied",
            }
        )
        if error.code == "authentication" and not expired:
            headers, _how = state.renew(headers, stage)
            try:
                media, current = fetch(url, headers)
            except ProbeError as again:
                if again.code == "authentication":
                    raise ProbeError("auth-expired").at(stage) from again
                raise
            return media, current, headers
        if expired:
            renewed = error.code in {"authentication", "denied"}
            if renewed:
                headers, _how = state.renew(headers, stage)
            result = open_source()
            if isinstance(result, ProbeError) and result.code == "authentication" and not renewed:
                headers, _how = state.renew(headers, stage)
                result = open_source()
            if isinstance(result, ProbeError):
                if result.code == "authentication":
                    raise ProbeError("auth-expired").at(stage) from result
                if _playlist_retryable(result):
                    raise result
                raise result.at(stage)
            current, media = result
            return media, current, headers
        raise


def _playlist_retryable(error) -> bool:
    """at を1段はがして、not-found / denied / network / http 408・5xx なら True。"""
    current = error.unwrap_at()
    return current.code in {"not-found", "denied"} or current.is_transient_http()


def _apply_playlist(updated, url, media, sequence, mode, control, headers):
    """新しい一覧を入れる。戻り値の末尾はデコーダの再初期化が必要か。"""
    if mode == "live":
        cursor = HlsCursor(True, media, sequence, None)
        cursor.install(updated)
        if control is not None:
            control.messages.extend(cursor.messages)
        cursor.messages.clear()
        return cursor.media, url, cursor.next_sequence, headers, cursor.pending_discontinuity
    if sequence < updated.media_sequence:
        raise ProbeError("window")
    return updated, url, sequence, headers, False


def _get_or_recover(
    url,
    send_headers,
    stage,
    state,
    headers,
    playlist_url,
    source,
    media,
    sequence,
    mode,
    cancelled,
    played,
    kind="media",
):
    """セグメントを取得し、401 なら再認証して同じ位置を再取得する。

    `state.renew` でヘッダを更新し、プレイリストを取り直してから init
    （kind="init" なら map_uri）またはメディアの対象を再要求する。再認証後も
    401 なら `ProbeError("auth-expired")`、位置が窓外なら `ProbeError("window")`。
    戻り値は (body, headers, media, playlist_url, sequence, 再取得したか)。
    """
    try:
        reply = stream_get(
            url,
            send_headers,
            limit=SAMPLE_LIMIT,
            cancelled=cancelled,
            redirects=True,
            readonly_retry=True,
        )
        return reply.body, headers, media, playlist_url, sequence, False
    except ProbeError as error:
        if error.code != "authentication":
            raise
        # 401 は再認証を1回だけ試し、同じ位置を取り直す（無限に再認証しない）。
        headers, _how = state.renew(headers, stage)
        try:
            updated, playlist_url, headers = _refresh_playlist(
                playlist_url, source, headers, cancelled, state, mode, played, stage
            )
            media, playlist_url, sequence, headers, reset = _apply_playlist(
                updated, playlist_url, media, sequence, mode, None, headers
            )
        except ProbeError as again:
            if again.code == "authentication":
                raise ProbeError("auth-expired").at(stage) from again
            raise
        segment = _segment_at(media, sequence)
        if segment is None:
            raise ProbeError("window")
        target = segment.map_uri if kind == "init" and segment.map_uri else segment.uri
        retry = _join(playlist_url, target)
        try:
            reply = stream_get(
                retry,
                headers,
                limit=SAMPLE_LIMIT,
                cancelled=cancelled,
                redirects=True,
                readonly_retry=True,
            )
        except ProbeError as again:
            if again.code == "authentication":
                raise ProbeError("auth-expired").at(stage) from again
            raise
        return reply.body, headers, media, playlist_url, sequence, reset


def play_station(
    station: str,
    mode: str,
    cursor: datetime,
    now: datetime,
    session: MemberSession,
    membership: Membership,
    position: float,
    limit: float | None,
    cancelled,
    commands=None,
    reconnect_sleep=None,
    reauth_now=None,
    on_notice=None,
    transport_sleep=None,
    transport_delays=None,
    on_ready=None,
) -> PlayReport:
    """再生を実行して PlayReport を返す公開ラッパー。

    `transport_sleep` / `transport_delays` は通信の待機を差し替えるテスト用。
    実体は `_play_station`。
    """
    with transport_overrides(transport_sleep, transport_delays):
        return _play_station(
            station,
            mode,
            cursor,
            now,
            session,
            membership,
            position,
            limit,
            cancelled,
            commands,
            reconnect_sleep,
            reauth_now,
            on_notice,
            on_ready,
        )


def _validate_seek_window(media, program, position: float) -> None:
    """シーク位置が窓の先頭より前、または 10秒+target_duration+1 より後なら playlist。"""
    first = segment_wall(media, media.media_sequence)
    if first is None:
        raise ProbeError("playlist").at("シーク時刻との対応を確認できません")
    target = program.start + timedelta(seconds=position)
    lead = (target - first).total_seconds()
    if lead < 0:
        raise ProbeError("playlist").at("指定位置より後の配信です")
    if lead > 10 + media.target_duration + 1:
        raise ProbeError("playlist").at("シーク先に対応しない配信です")


def _play_station(
    station: str,
    mode: str,
    cursor: datetime,
    now: datetime,
    session: MemberSession,
    membership: Membership,
    position: float,
    limit: float | None,
    cancelled,
    commands=None,
    reconnect_sleep=None,
    reauth_now=None,
    on_notice=None,
    on_ready=None,
) -> PlayReport:
    """HLS を取得・復号・出力し、終了理由を PlayReport にまとめる。

    `cancelled()` か停止で `"cancelled"`、`limit` 到達で `"limit"`、ENDLIST 終端で
    `"program-end"` を返し、回復不能な失敗は `ProbeError` を送出する。
    """
    # 切替・シークをまたいで操作メッセージと音量を持ち越す。
    carried: list[str] = []
    carried_volume: int | None = None
    seek_prep = None
    queue: list[str] | None = list(commands) if isinstance(commands, list) else None
    all_taken = 0
    all_frames = 0
    all_underrun = 0
    while True:
        # シーク後は prepare 済みの結果を再利用し、余計な局一覧取得をしない。
        if seek_prep is None:
            program, headers, _area, _outside, urls = prepare(
                station, mode, cursor, now, session, membership, position, cancelled
            )
            if on_ready is not None:
                on_ready(program)
                on_ready = None
            if not urls:
                raise ProbeError("endpoint")
            playlist_url, media, source = _open_endpoints(urls, headers, cancelled)
        else:
            program, headers, playlist_url, media, source = seek_prep
            seek_prep = None
        if position:
            _validate_seek_window(media, program, position)
        reauth = ReauthState(session, cancelled, authenticate_stream, now=reauth_now)

        duration = (program.end - program.start).total_seconds()
        control = InteractiveControl(duration, mode, station)
        control.messages.extend(carried)
        carried = []
        if carried_volume is not None:
            control.volume, carried_volume = carried_volume, None
        control.position = position
        budget = ReconnectBudget(cancelled, sleep=reconnect_sleep)
        stamp, codec = None, getattr(media, "declared_codec", None)
        rate, channels = 48000, 2
        taken, played_frames, underrun_frames, media_seconds = 0, 0, 0, 0.0
        reason = "program-end"
        decoder, fragment_decoder, init_uri, discontinuity = AdtsDecoder(), None, None, None
        sequence = media.media_sequence
        pending = (program.start + timedelta(seconds=position), position) if position else None
        if position:
            media_seconds = position
        stream = None
        stop_playback = _StopView(
            lambda: cancelled() or control.stopped or control.selected is not None
        )

        def close_stream() -> None:
            """出力ストリームを閉じ、フレーム数とアンダーランを累計へ足して未使用にする。"""
            nonlocal stream, played_frames, underrun_frames
            if stream is not None:
                stream.close()
                summary = stream.summary()
                played_frames += summary.frames
                underrun_frames += summary.underrun_frames
                stream = None

        def emit(piece, rate: int, channels: int) -> None:
            """PCM を出力する。ストリームが無いか rate/channels が変われば開き直し、音量を渡す。"""
            nonlocal stream
            if not len(piece):
                return
            if stream is None or (stream.rate, stream.input_channels) != (rate, channels):
                close_stream()
                stream = output.open_stream(rate, channels, stop_playback, step_hook=drain)
            stream.write(piece, volume=control.volume)

        def drain(*_args) -> None:
            """操作コマンドを空になるまで処理し、新しい通知を on_notice へ流す。停止・局切替で打ち切る。"""
            while True:
                line = commands() if callable(commands) else (queue.pop(0) if queue else None)
                if not line:
                    return
                before = len(control.messages)
                control.handle(line)
                if on_notice is not None:
                    for message in control.messages[before:]:
                        on_notice(message)
                if control.stopped or control.selected is not None:
                    return

        def reset_state() -> None:
            """不連続や窓の飛び越しで、デコーダと時刻照合を捨てる。"""
            nonlocal decoder, fragment_decoder, init_uri, stamp
            decoder, fragment_decoder, init_uri, stamp = AdtsDecoder(), None, None, None

        switching, reseeking = False, False
        try:
            while True:
                if cancelled() or control.stopped:
                    reason = "cancelled"
                    break
                drain()
                # 局切替は内側ループを抜け、prepare からやり直してデコーダも作り直す。
                if control.selected is not None:
                    new_mode, new_station, new_cursor, new_offset = control.selected
                    control.selected = None
                    close_stream()
                    if queue is not None:
                        queue.clear()
                    now = datetime.now(timezone.utc)
                    station, mode, cursor, position = new_station, new_mode, new_cursor, new_offset
                    on_ready, switching = None, True
                    break
                if control.stopped or cancelled():
                    reason = "cancelled"
                    break
                if control.paused:
                    time.sleep(0.01)
                    continue
                sequence, media_seconds, pending, restart = _apply_seek(
                    control, media, sequence, media_seconds, program.start, pending
                )
                if restart is not None:
                    try:
                        new_program, new_headers, _a, _o, new_urls = prepare(
                            station, mode, cursor, now, session, membership, restart, cancelled
                        )
                        if not new_urls:
                            raise ProbeError("endpoint")
                        new_url, new_media, new_source = _open_endpoints(
                            new_urls, new_headers, cancelled
                        )
                        _validate_seek_window(new_media, new_program, restart)
                    except ProbeError:
                        before = len(control.messages)
                        control.messages.append("再生準備に失敗しました（現在の再生を継続）")
                        if on_notice is not None:
                            for message in control.messages[before:]:
                                on_notice(message)
                        continue
                    position = restart
                    seek_prep = (new_program, new_headers, new_url, new_media, new_source)
                    carried_volume = control.volume
                    reseeking = True
                    break
                if sequence >= media.media_sequence + len(media.segments):
                    # ENDLIST が無いライブは窓を読み直して続ける。
                    if media.end_list:
                        reason = "program-end"
                        break
                    try:
                        media, playlist_url, sequence, headers, reset = _reload_window(
                            source,
                            playlist_url,
                            headers,
                            cancelled,
                            media,
                            sequence,
                            mode,
                            control,
                            reauth,
                            taken > 0,
                        )
                        if reset:
                            reset_state()
                    except ProbeError as error:
                        if _cancelled_error(error) or cancelled():
                            reason = "cancelled"
                            break
                        # 再生開始後の通信失敗だけ再接続する。開始前は prepare 側で失敗させる。
                        if budget.can_retry(error, taken > 0) or (
                            taken > 0 and mode == "live" and _playlist_retryable(error)
                        ):
                            budget.wait()
                            continue
                        raise
                    continue
                marker = _segment_discontinuity(media, sequence)
                if discontinuity is not None and marker != discontinuity:
                    reset_state()
                discontinuity = marker
                segment = _segment_at(media, sequence)
                if segment is None:
                    raise ProbeError("window")
                fetched = _fetch_segment(
                    segment,
                    media,
                    sequence,
                    playlist_url,
                    source,
                    headers,
                    mode,
                    control,
                    reauth,
                    budget,
                    cancelled,
                    taken > 0,
                    fragment_decoder,
                    init_uri,
                )
                headers, media, playlist_url = fetched.headers, fetched.media, fetched.playlist_url
                sequence, segment = fetched.sequence, fetched.segment
                fragment_decoder, init_uri = fetched.fragment_decoder, fetched.init_uri
                if fetched.reset:
                    reset_state()
                if fetched.cancelled:
                    reason = "cancelled"
                    break
                if fetched.retry:
                    continue
                reauth.episode = False
                codec = getattr(media, "declared_codec", None) or codec
                # limit の切り詰めは _decode_segment が行い、stop/limit_hit でここが打ち切る。
                decoded_segment = _decode_segment(
                    fetched.payload,
                    segment,
                    media,
                    sequence,
                    codec,
                    decoder,
                    fragment_decoder,
                    stamp,
                    pending,
                    limit,
                    media_seconds,
                    cancelled,
                )
                stamp = decoded_segment.stamp
                rate, channels = decoded_segment.rate, decoded_segment.channels
                if decoded_segment.discard:
                    sequence += 1
                    continue
                if decoded_segment.stop:
                    reason = "limit"
                    break
                if pending is not None:
                    pending = None
                if len(decoded_segment.samples):
                    emit(decoded_segment.samples, rate, channels)
                media_seconds = (
                    decoded_segment.media_seconds + len(decoded_segment.samples) / channels / rate
                )
                taken += 1
                sequence += 1
                budget.reset()
                if decoded_segment.limit_hit or (limit is not None and media_seconds >= limit):
                    reason = "limit"
                    break
        finally:
            close_stream()
        all_taken += taken
        all_frames += played_frames
        all_underrun += underrun_frames
        if reseeking:
            carried, on_ready = control.messages, None
            continue
        if switching:
            all_taken, all_frames, all_underrun, carried_volume = 0, 0, 0, None
            continue
        if cancelled():
            reason = "cancelled"
        return PlayReport(
            reason,
            all_frames,
            rate,
            channels,
            all_taken,
            all_underrun,
            program.title,
            station,
            "Live" if mode == "live" else "TimeFree",
            program.start,
            program.end,
            control.messages,
        )


@dataclass
class _FetchedSegment:
    """1セグメントの取得結果。`cancelled` は再生終了、`reset` はデコーダと時刻照合の
    リセット、`retry` は現周回をスキップして次周回へ進むことを示す。
    """

    payload: bytes
    headers: dict[str, str]
    media: object
    playlist_url: str
    sequence: int
    fragment_decoder: FragmentedAacDecoder | None
    init_uri: str | None
    segment: object
    reset: bool = False
    retry: bool = False
    cancelled: bool = False


def _fetch_segment(
    segment,
    media,
    sequence: int,
    playlist_url: str,
    source: str,
    headers: dict[str, str],
    mode: str,
    control: InteractiveControl,
    reauth: ReauthState,
    budget: ReconnectBudget,
    cancelled,
    played: bool,
    fragment_decoder: FragmentedAacDecoder | None,
    init_uri: str | None,
) -> _FetchedSegment:
    """初期化セグメントとメディアセグメントを取得し、失敗時は再接続を試みる。"""

    def state(payload: bytes = b"", **flags: bool) -> _FetchedSegment:
        """現在の局所状態をまとめた _FetchedSegment を返す。"""
        return _FetchedSegment(
            payload,
            headers,
            media,
            playlist_url,
            sequence,
            fragment_decoder,
            init_uri,
            segment,
            **flags,
        )

    def fetch(target: str, stage: str, kind: str = "media") -> tuple[bytes, bool]:
        """_get_or_recover を呼び、更新後のヘッダ・プレイリスト・位置を局所変数へ反映する。"""
        nonlocal headers, media, playlist_url, sequence
        body, headers, media, playlist_url, sequence, reset = _get_or_recover(
            target,
            {},
            stage,
            reauth,
            headers,
            playlist_url,
            source,
            media,
            sequence,
            mode,
            cancelled,
            played,
            kind,
        )
        return body, reset

    try:
        if segment.map_uri:
            resolved = _join(playlist_url, segment.map_uri)
            if resolved != init_uri:
                init_bytes, reset = fetch(resolved, "HLS初期化情報取得", "init")
                if reset:
                    return state(reset=True, retry=True)
                if not init_bytes:
                    raise ProbeError("playlist")
                fragment_decoder = FragmentedAacDecoder(init_bytes)
                segment = _segment_at(media, sequence)
                if segment is None:
                    raise ProbeError("window")
                init_uri = _join(playlist_url, segment.map_uri) if segment.map_uri else None
        payload, reset = fetch(_join(playlist_url, segment.uri), "HLS音声取得")
        if reset:
            return state(reset=True, retry=True)
        if not payload:
            raise ProbeError("playlist")
        return state(payload)
    except ProbeError as error:
        if _cancelled_error(error):
            return state(cancelled=True)
        if not budget.can_retry(error, played):
            raise
        try:
            budget.wait()
            latest, latest_url, latest_headers = _refresh_playlist(
                playlist_url, source, headers, cancelled, reauth, mode, True, "HLS一覧更新"
            )
            media, playlist_url, sequence, headers, reset = _apply_playlist(
                latest, latest_url, media, sequence, mode, control, latest_headers
            )
        except ProbeError as waited:
            if _cancelled_error(waited):
                return state(cancelled=True)
            if budget.can_retry(waited, True):
                return state(retry=True)
            raise
        return state(reset=reset, retry=True)


@dataclass
class _DecodedSegment:
    """1セグメントの復号結果。

    `media_seconds` はトリム後・limit 切り詰め後の経過秒。`limit_hit` は limit で
    打ち切る必要があることを示す。`stop` は残りが 0 秒で、出力せずに打ち切ることを示す。
    `discard` はトリムでセグメント全体を捨てることを示す（復号後の状態は保持する）。
    """

    samples: np.ndarray
    rate: int
    channels: int
    frames: int
    stamp: tuple[int, datetime] | None
    media_seconds: float
    limit_hit: bool
    stop: bool = False
    discard: bool = False


def _decode_segment(
    payload: bytes,
    segment,
    media,
    sequence: int,
    codec: str | None,
    decoder: AdtsDecoder,
    fragment_decoder: FragmentedAacDecoder | None,
    stamp: tuple[int, datetime] | None,
    pending: tuple[datetime, float] | None,
    limit: float | None,
    media_seconds: float,
    cancelled,
) -> _DecodedSegment:
    """1セグメントを復号し、シークのトリムと limit の切り詰めを適用する。

    トリムでセグメント全体を捨てる場合は discard=True を返す。
    """
    if fragment_decoder is not None and (
        payload[4:8] in {b"moof", b"styp"} or (len(payload) > 8 and payload[4:8] == b"moof")
    ):
        decoded = fragment_decoder.decode_pcm(payload, cancelled, codec)
        piece = decoded.samples
        rate = decoded.sample_rate
        channels = decoded.channels
        frame_count = decoded.frames
        stamp = accept_fragment(
            segment.duration,
            frame_count / rate,
            decoded.decode_time,
            decoded.timescale,
            segment_wall(media, sequence),
            stamp,
        )
    else:
        pcm = decoder.decode_segment_pcm(payload, codec, cancelled)
        piece = pcm.samples
        rate = pcm.summary.sample_rate
        channels = pcm.summary.channels
        frame_count = pcm.summary.frames
    if pending is not None:
        target_at, target_seconds = pending
        trimmed = trim_pcm(piece, channels, rate, segment_wall(media, sequence), target_at)
        if trimmed is None:
            empty = piece[:0]
            return _DecodedSegment(
                empty, rate, channels, 0, stamp, media_seconds, False, False, True
            )
        piece, frame_count = trimmed
        media_seconds = target_seconds
    if limit is not None:
        room = limit - media_seconds
        if room <= 0:
            empty = piece[:0]
            return _DecodedSegment(empty, rate, channels, 0, stamp, media_seconds, True, True)
        max_frames = int(room * rate)
        if frame_count > max_frames:
            piece = piece[: max_frames * channels]
            return _DecodedSegment(piece, rate, channels, max_frames, stamp, media_seconds, True)
    return _DecodedSegment(piece, rate, channels, frame_count, stamp, media_seconds, False)


class _StopView:
    """0引数の述語を `is_set()` 付きの停止オブジェクトに包む。出力ストリームへ渡す
    `stop` として使う。
    """

    def __init__(self, function) -> None:
        """停止判定の0引数関数を保持する。"""
        self.function = function

    def is_set(self) -> bool:
        """保持した関数を呼び、停止なら True。"""
        return self.function()


def _cancelled_error(error) -> bool:
    """code が cancelled、または at の inner が cancelled なら True。"""
    return error.code == "cancelled" or (
        error.code == "at" and error.inner is not None and error.inner.code == "cancelled"
    )


def _reload_window(source, url, headers, cancelled, media, sequence, mode, control, reauth, played):
    """target_duration/2（最低50ms）待って一覧を取り直し、窓と次位置を更新する。"""
    wait = max(media.target_duration / 2.0, 0.05)
    end = time.monotonic() + wait
    while True:
        if cancelled():
            raise ProbeError("cancelled")
        left = end - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(0.05, left))
    updated, url, headers = _refresh_playlist(
        url, source, headers, cancelled, reauth, mode, played, "HLS一覧更新"
    )
    media, url, sequence, headers, reset = _apply_playlist(
        updated, url, media, sequence, mode, control, headers
    )
    return media, url, sequence, headers, reset


def _open_endpoints(urls, headers, cancelled):
    """配信先を順に試し、最初に開けたものとその source URL を返す。"""
    last = ProbeError("endpoint")
    for candidate in urls:
        try:
            playlist_url, media = _open_media(candidate, headers, cancelled)
        except ProbeError as error:
            if error.code in {"authentication", "limited"}:
                raise
            last = error
            continue
        return playlist_url, media, candidate
    raise last.at("連続配信の準備")


def _open_media(url: str, headers: dict[str, str], cancelled):
    """マスターを最大4回たどってメディアプレイリストを開き、(最終 URL, media) を返す。"""
    current = url
    codec = None
    for _ in range(4):
        reply = stream_get(
            current,
            headers,
            limit=TEXT_LIMIT,
            cancelled=cancelled,
            redirects=True,
            readonly_retry=True,
        )
        text = reply.body.decode()
        current = reply.url
        if "#EXT-X-STREAM-INF:" in text and "#EXTINF:" not in text:
            codec, current = _master_target(text, current)
            continue
        media = parse_media_playlist(text)
        media.declared_codec = codec
        return current, media
    raise ProbeError("playlist")


def inspect_remote_chapters(station: str, cursor: datetime, cancelled=None) -> ChapterView:
    """番組のチャプターを取得して ChapterView を返す。

    取り消しは `ChapterFailure`。番組取得の失敗も `ChapterFailure` に包む。
    eventId と番組開始位置が揃わない場合は kind="unresolved" で理由を返し、
    提供がない場合は kind="unavailable" にする。
    """
    cancelled = cancelled or (lambda: False)
    if cancelled():
        raise ChapterFailure("チャプター取得を取り消しました")
    try:
        program = fetch_program(station, cursor, cancelled)
    except ProbeError as error:
        raise ChapterFailure(f"チャプター取得失敗: {error}") from error
    view = ChapterView(
        station,
        program.title,
        program.start,
        program.end,
        "unresolved",
        "番組との対応付け未確認（イベント情報を確認できません）",
    )
    if program.event_id is None or program.origin is None:
        return view
    if program.origin != program.start:
        view.issue = "番組との対応付け未確認（開始位置のずれは補正未対応）"
        return view
    if cancelled():
        raise ChapterFailure("チャプター取得を取り消しました")
    try:
        reply = stream_get(
            f"https://api.annex-cf.radiko.jp/v1/programs/{program.event_id}/chapters",
            limit=1024 * 1024,
            cancelled=cancelled,
        )
    except ProbeError as error:
        raise ChapterFailure(f"チャプター取得失敗: {error}") from error
    try:
        root = json.loads(reply.body.decode())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ChapterFailure("チャプター解析失敗: チャプターJSONを解析できません") from error
    if (
        not isinstance(root, dict)
        or root.get("stationId") != station
        or root.get("programId") != program.event_id
    ):
        raise ChapterFailure("チャプター応答の局・イベントが対象と一致しません")
    duration = (program.end - program.start).total_seconds()
    try:
        if len(reply.body) > MAX_CHAPTER_BYTES:
            raise ChapterError("large")
        parsed = chapters_from_json(root, duration, 0.0)
    except ChapterError as error:
        raise ChapterFailure(f"チャプター解析失敗: {error}") from error
    if not parsed.chapters and parsed.invalid_count == 0:
        view.kind = "unavailable"
        view.issue = None
        return view
    binding = ProgramKey(station, program.start)
    if binding.starts_at != program.origin:
        view.kind = "unresolved"
        view.issue = "番組との対応付け未確認（放送枠が一致しません）"
        return view
    view.kind = "aligned"
    view.issue = None
    view.count = len(parsed.chapters)
    view.music = parsed.music_count
    view.invalid = parsed.invalid_count
    view.rows = [(item.start, item.end, item.title) for item in parsed.chapters]
    view.summaries = [item.summary for item in parsed.chapters]
    return view
