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

from radiko import output
from radiko.auth import partial_key
from radiko.catalog import CatalogError, parse_endpoints, parse_stations
from radiko.chapters import ChapterError, parse_chapters
from radiko.domain import JST, ProgramKey, broadcast_day
from radiko.fragmented import FragmentedAacDecoder
from radiko.hls import HlsCursor, parse_media_playlist
from radiko.http_client import stream_get, transport_overrides
from radiko.interactive import InteractiveControl
from radiko.media import AacError, AdtsDecoder, id3_offset
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
    title: str | None
    start: datetime
    end: datetime
    timefree: tuple[bool | None, bool | None, bool | None, bool | None]
    failed_record: bool | None
    event_id: str | None
    origin: datetime | None
    station_id: str

    def slot(self) -> SlotProgram:
        return SlotProgram(self.start, self.end, self.timefree, self.failed_record)


@dataclass
class ProbeReport:
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
    samples: list[float] | None
    title: str | None

    def __repr__(self) -> str:
        return f"ProbeReport(station={self.station!r}, mode={self.mode!r}, samples=[REDACTED])"


@dataclass
class PlayReport:
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
    def __init__(self, text: str) -> None:
        super().__init__(text)


@dataclass
class ChapterView:
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
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_program(data: bytes, station: str, cursor: datetime) -> ResolvedProgram:
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
    headers = {
        "x-radiko-app": "pc_html5",
        "x-radiko-app-version": "0.0.1",
        "x-radiko-device": "pc",
        "x-radiko-user": "dummy_user",
        "cookie": f"radiko_session={session.token}",
    }
    first = stream_get(f"{API}/v2/api/auth1", headers, limit=64 * 1024, cancelled=cancelled)
    token = _header(first.headers, "x-radiko-authtoken")
    try:
        offset = int(_header(first.headers, "x-radiko-keyoffset"))
        length = int(_header(first.headers, "x-radiko-keylength"))
        partial = partial_key(offset, length)
    except (ValueError, ProbeError) as error:
        raise ProbeError("auth-format") from error
    second_headers = {
        **headers,
        "x-radiko-authtoken": token,
        "x-radiko-partialkey": partial,
        "x-radiko-session": session.token,
        "x-radiko-connection": "wifi",
    }
    second = stream_get(f"{API}/v2/api/auth2", second_headers, limit=64 * 1024, cancelled=cancelled)
    area_raw = second.body.decode().strip().split(",")[0]
    if not area_raw.startswith("JP"):
        raise ProbeError("outside-japan")
    try:
        number = int(area_raw[2:])
    except ValueError as error:
        raise ProbeError("outside-japan") from error
    if not 1 <= number <= 47:
        raise ProbeError("outside-japan")
    area = f"JP{number}"
    return {"x-radiko-authtoken": token, "x-radiko-areaid": area}, area


def _header(headers: dict[str, str], name: str) -> str:
    value = headers.get(name.lower())
    if not value:
        raise ProbeError("auth-format")
    return value


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
    for line in text.splitlines():
        if line.startswith("#EXT-X-SESSION-KEY:"):
            method = _attr(line, "METHOD")
            if method not in {None, "NONE"}:
                return True
    return False


def _master_target(text: str, base: str) -> tuple[str | None, str]:
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
    codec = _safe_codec(_attr(chosen_attr, "CODECS"))
    group = _attr(chosen_attr, "AUDIO")
    uri = chosen_uri
    if group:
        for item in alternatives:
            if (
                _attr(item, "TYPE") == "AUDIO"
                and _attr(item, "GROUP-ID") == group
                and _attr(item, "URI")
            ):
                uri = _attr(item, "URI") or uri
                break
    return codec, _join(base, uri)


def _safe_codec(text: str | None) -> str | None:
    if text is None:
        return None
    return (
        text
        if len(text) <= 100
        and all(char.isascii() and (char.isalnum() or char in ".,-_ ") for char in text)
        else None
    )


def _bandwidth(line: str) -> int:
    raw = _attr(line, "BANDWIDTH") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


def _attr(line: str, name: str) -> str | None:
    marker = f"{name}="
    start = line.find(marker)
    if start < 0:
        return None
    rest = line[start + len(marker) :]
    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end]
    return rest.split(",", 1)[0]


def _join(base: str, uri: str) -> str:
    return urljoin(base, uri)


def _sample(
    media, playlist_url: str, headers, codec, retain: bool, cancelled, encrypted: bool = False
) -> ProbeReport:
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
    blob = sample + segment
    container = "encrypted" if encrypted or first.encrypted else container_name(blob)
    packets = frames = rate = channels = core_rate = core_channels = 0
    sbr = stereo = False
    media_duration = decode_duration = 0.0
    nonzero = 0
    decode_error = None
    samples = None
    offset = id3_offset(blob)
    audio = blob[offset:] if offset is not None else b""
    adts = len(audio) >= 2 and audio[0] == 0xFF and (audio[1] & 0xF0) == 0xF0
    if container == "encrypted":
        decode_error = None
    elif container.startswith("ISO BMFF") and sample:
        started = time.perf_counter()
        try:
            core = FragmentedAacDecoder(sample)
            decoded = core.decode_pcm(segment, cancelled, codec)
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
        container=container,
        sample_bytes=len(blob),
        packets=packets,
        frames=frames,
        sample_rate=rate,
        channels=channels,
        core_sample_rate=core_rate,
        core_channels=core_channels,
        sbr_detected=sbr,
        stereo_expansion=stereo,
        media_duration=media_duration,
        decode_duration=decode_duration,
        nonzero_samples=nonzero,
        decode_error=decode_error,
        samples=samples,
        title=None,
    )


def container_name(data: bytes) -> str:
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
    cursor = media.media_sequence
    walls = []
    for segment in media.segments:
        wall = segment_wall(media, cursor)
        if wall is not None:
            walls.append((wall, segment.duration))
        cursor += 1
    if walls and (
        target_at < walls[0][0] or target_at >= walls[-1][0] + timedelta(seconds=walls[-1][1])
    ):
        return sequence, media_seconds, pending, target
    cursor = media.media_sequence
    for segment in media.segments:
        wall = segment_wall(media, cursor)
        if wall is not None and target_at >= wall + timedelta(seconds=segment.duration):
            cursor += 1
            continue
        break
    return cursor, target, (target_at, target), None


def _segment_at(media, sequence: int):
    start = media.media_sequence
    if sequence < start or sequence >= start + len(media.segments):
        return None
    return media.segments[sequence - start]


def _segment_discontinuity(media, sequence: int):
    base = getattr(media, "discontinuity_sequence", 0)
    count = 0
    cursor = media.media_sequence
    for segment in media.segments:
        if segment.discontinuity:
            count += 1
        if cursor == sequence:
            return base + count
        cursor += 1
    return None


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
    current = error.inner if error.code == "at" and error.inner is not None else error
    if current.code in {"not-found", "denied", "network"}:
        return True
    return current.code == "http" and (current.status == 408 or (current.status or 0) >= 500)


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
    carried: list[str] = []
    carried_volume: int | None = None
    seek_prep = None
    queue: list[str] | None = list(commands) if isinstance(commands, list) else None
    all_taken = 0
    all_frames = 0
    all_underrun = 0
    while True:
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
        from radiko.reauth import ReauthState

        reauth = ReauthState(session, cancelled, now=reauth_now)

        duration = (program.end - program.start).total_seconds()
        control = InteractiveControl(duration, mode, station)
        control.messages.extend(carried)
        carried = []
        if carried_volume is not None:
            control.volume = carried_volume
            carried_volume = None
        control.position = position
        budget = ReconnectBudget(cancelled, sleep=reconnect_sleep)
        stamp = None
        codec = getattr(media, "declared_codec", None)
        rate = 48000
        channels = 2
        taken = 0
        played_frames = 0
        underrun_frames = 0
        media_seconds = 0.0
        reason = "program-end"
        decoder = AdtsDecoder()
        fragment_decoder = None
        init_uri = None
        discontinuity = None
        sequence = media.media_sequence
        pending = None
        if position:
            pending = (program.start + timedelta(seconds=position), position)
            media_seconds = position
        stream = None
        stop_playback = _StopView(
            lambda: cancelled() or control.stopped or control.selected is not None
        )

        def close_stream() -> None:
            nonlocal stream, played_frames, underrun_frames
            if stream is not None:
                stream.close()
                summary = stream.summary()
                played_frames += summary.frames
                underrun_frames += summary.underrun_frames
                stream = None

        def emit(piece, rate: int, channels: int) -> None:
            nonlocal stream
            if not len(piece):
                return
            if stream is None or (stream.rate, stream.input_channels) != (rate, channels):
                close_stream()
                stream = output.open_stream(rate, channels, stop_playback, step_hook=drain)
            stream.write(piece, volume=control.volume)

        def drain(*_args) -> None:
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
            decoder = AdtsDecoder()
            fragment_decoder = None
            init_uri = None
            stamp = None

        switching = False
        reseeking = False
        try:
            while True:
                if cancelled() or control.stopped:
                    reason = "cancelled"
                    break
                drain()
                if control.selected is not None:
                    new_mode, new_station, new_cursor, new_offset = control.selected
                    control.selected = None
                    close_stream()
                    if queue is not None:
                        queue.clear()
                    station, mode, cursor, now, position = (
                        new_station,
                        new_mode,
                        new_cursor,
                        datetime.now(timezone.utc),
                        new_offset,
                    )
                    on_ready = None
                    switching = True
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
                try:
                    if segment.map_uri:
                        resolved = _join(playlist_url, segment.map_uri)
                        if resolved != init_uri:
                            init_bytes, headers, media, playlist_url, sequence, reset = (
                                _get_or_recover(
                                    resolved,
                                    {},
                                    "HLS初期化情報取得",
                                    reauth,
                                    headers,
                                    playlist_url,
                                    source,
                                    media,
                                    sequence,
                                    mode,
                                    cancelled,
                                    taken > 0,
                                    "init",
                                )
                            )
                            if reset:
                                reset_state()
                                continue
                            if not init_bytes:
                                raise ProbeError("playlist")
                            fragment_decoder = FragmentedAacDecoder(init_bytes)
                            segment = _segment_at(media, sequence)
                            if segment is None:
                                raise ProbeError("window")
                            init_uri = (
                                _join(playlist_url, segment.map_uri) if segment.map_uri else None
                            )
                    payload, headers, media, playlist_url, sequence, reset = _get_or_recover(
                        _join(playlist_url, segment.uri),
                        {},
                        "HLS音声取得",
                        reauth,
                        headers,
                        playlist_url,
                        source,
                        media,
                        sequence,
                        mode,
                        cancelled,
                        taken > 0,
                    )
                    if reset:
                        reset_state()
                        continue
                    if not payload:
                        raise ProbeError("playlist")
                except ProbeError as error:
                    if _cancelled_error(error):
                        reason = "cancelled"
                        break
                    if budget.can_retry(error, taken > 0):
                        try:
                            budget.wait()
                            latest, latest_url, latest_headers = _refresh_playlist(
                                playlist_url,
                                source,
                                headers,
                                cancelled,
                                reauth,
                                mode,
                                True,
                                "HLS一覧更新",
                            )
                            media, playlist_url, sequence, headers, reset = _apply_playlist(
                                latest,
                                latest_url,
                                media,
                                sequence,
                                mode,
                                control,
                                latest_headers,
                            )
                            if reset:
                                reset_state()
                        except ProbeError as waited:
                            if _cancelled_error(waited):
                                reason = "cancelled"
                                break
                            if budget.can_retry(waited, True):
                                continue
                            raise
                        continue
                    raise
                reauth.episode = False
                codec = getattr(media, "declared_codec", None) or codec
                if fragment_decoder is not None and (
                    payload[4:8] in {b"moof", b"styp"}
                    or (len(payload) > 8 and payload[4:8] == b"moof")
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
                    trimmed = trim_pcm(
                        piece, channels, rate, segment_wall(media, sequence), target_at
                    )
                    if trimmed is None:
                        sequence += 1
                        continue
                    piece, frame_count = trimmed
                    media_seconds = target_seconds
                    pending = None
                if limit is not None:
                    room = limit - media_seconds
                    if room <= 0:
                        reason = "limit"
                        break
                    max_frames = int(room * rate)
                    if frame_count > max_frames:
                        piece = piece[: max_frames * channels]
                        emit(piece, rate, channels)
                        taken += 1
                        sequence += 1
                        reason = "limit"
                        break
                if len(piece):
                    emit(piece, rate, channels)
                media_seconds += len(piece) / channels / rate
                taken += 1
                sequence += 1
                budget.reset()
                if limit is not None and media_seconds >= limit:
                    reason = "limit"
                    break
        finally:
            close_stream()
        all_taken += taken
        all_frames += played_frames
        all_underrun += underrun_frames
        if reseeking:
            carried = control.messages
            on_ready = None
            continue
        if switching:
            all_taken = 0
            all_frames = 0
            all_underrun = 0
            carried_volume = None
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


class _StopView:
    def __init__(self, function) -> None:
        self.function = function

    def is_set(self) -> bool:
        return self.function()


def _cancelled_error(error) -> bool:
    return error.code == "cancelled" or (
        error.code == "at" and error.inner is not None and error.inner.code == "cancelled"
    )


def _reload_window(source, url, headers, cancelled, media, sequence, mode, control, reauth, played):
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
    if root.get("stationId") != station or root.get("programId") != program.event_id:
        raise ChapterFailure("チャプター応答の局・イベントが対象と一致しません")
    duration = (program.end - program.start).total_seconds()
    try:
        parsed = parse_chapters(reply.body, duration, 0.0)
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
