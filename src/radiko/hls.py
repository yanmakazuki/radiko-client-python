"""HLS メディアプレイリストの窓。通し番号で新規セグメントを決め、署名の変化は別セグメントにしない。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from radiko.stream import ProbeError

TEXT_LIMIT = 1024 * 1024
MAX_ITEMS = 4096


@dataclass
class Segment:
    """HLS の1セグメント。URI・継続時間と、このセグメントから効く DISCONTINUITY・
    PROGRAM-DATE-TIME・MAP・KEY の状態を持つ。BYTERANGE・暗号化・不正 MAP は検証で拒否する印。
    """

    uri: str
    duration: float
    discontinuity: bool = False
    program_date: str | None = None
    map_uri: str | None = None
    byte_range: bool = False
    encrypted: bool = False
    bad_map: bool = False


@dataclass
class MediaPlaylist:
    """メディアプレイリストの窓。先頭セグメントの通し番号・target_duration・ENDLIST・
    不連続番号・version・宣言コーデックを持つ。
    """

    media_sequence: int
    target_duration: int
    segments: list[Segment]
    end_list: bool = False
    version: int | None = None
    i_frames_only: bool = False
    discontinuity_sequence: int = 0
    declared_codec: str | None = None
    _schedule: SegmentSchedule | None = field(default=None, init=False, repr=False, compare=False)


@dataclass
class SegmentSchedule:
    """1パスで求めた各セグメントの番組時刻と累積不連続番号。"""

    segments: list[Segment]
    media_sequence: int
    base: int
    walls: list[datetime | None]
    discontinuities: list[int]


@dataclass
class HlsCursor:
    """再生中のプレイリスト窓。ライブの追いつきとタイムフリーの窓喪失を分ける。"""

    live: bool
    media: MediaPlaylist
    next_sequence: int
    last_played: int | None = None
    skipped: int = 0
    resets: int = 0
    joined_head: bool = False
    pending_discontinuity: bool = False
    messages: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, media: MediaPlaylist, live: bool) -> HlsCursor:
        """検証済みの窓からカーソルを作る。ライブなら live_join_index まで進めて開始する。"""
        validate_media(media)
        cursor = cls(live, media, media.media_sequence)
        if live:
            cursor._realign(False)
        return cursor

    def install(self, media: MediaPlaylist) -> float:
        """新しい窓を取り込み、次に取得するまでの待ち秒を返す。VOD は開始番号が戻れば playlist、
        next_sequence が窓より前なら window。ライブは巻き戻り・前進を再配置する。
        """
        validate_media(media)
        previous = self.media.media_sequence
        if media.media_sequence < previous and not self.live:
            raise ProbeError("playlist")
        changed = media.media_sequence != previous or len(media.segments) != len(
            self.media.segments
        )
        wait = media.target_duration / (1.0 if changed else 2.0)
        self.media = media
        if self.live:
            reset = self._live_realignment(previous)
            if reset is not None:
                self._realign(reset)
        elif self.next_sequence < media.media_sequence:
            raise ProbeError("window")
        return wait

    def take(self) -> Segment | None:
        """次のセグメントを返して通し番号を進める。窓の外なら None を返し、位置は変えない。"""
        start = self.media.media_sequence
        end = start + len(self.media.segments)
        if not start <= self.next_sequence < end:
            return None
        segment = self.media.segments[self.next_sequence - start]
        self.last_played = self.next_sequence
        self.next_sequence += 1
        return segment

    def _live_realignment(self, previous: int) -> bool | None:
        """ライブの窓更新で再配置が必要か返す。巻き戻りが古ければ None、それ以外の巻き戻りは True。
        前進は窓より大きい欠落なら True、そうでなければ False。
        """
        start = self.media.media_sequence
        length = len(self.media.segments)
        end = start + length
        if start < previous:
            if self._rewind_is_stale(start, end, length):
                return None
            return True
        if self.next_sequence < start:
            return _forward_gap_is_reset(self.next_sequence, self.media)
        return None

    def _rewind_is_stale(self, start: int, end: int, length: int) -> bool:
        """巻き戻った窓が next_sequence を含むか、next_sequence がその末尾、または直近の再生位置が
        窓の終端付近なら True。古い窓として無視する。
        """
        contains_next = start <= self.next_sequence < end
        if contains_next or end == self.next_sequence:
            return True
        if self.last_played is None:
            return False
        tail = end - 1
        if start < self.last_played < end:
            return True
        return self.last_played >= tail and self.last_played - tail <= length

    def _realign(self, reset: bool) -> None:
        """ライブ端へ再配置する。reset ならリセット回数を増やし、飛ばした数と通知を更新する。
        joined_head は窓の先頭に参加したか、pending_discontinuity は次の再生前に通知する印。
        """
        index = live_join_index(self.media)
        target = self.media.media_sequence + index
        skipped = index if reset else target - self.next_sequence
        if skipped > 0:
            self.skipped += skipped
        if reset:
            self.resets += 1
        self.joined_head = index == 0
        self.next_sequence = target
        self.pending_discontinuity = True
        if skipped > 0 and self.live:
            if index == 0:
                self.messages.append(f"配信窓の先頭から再開しました（累計{self.skipped}）")
            else:
                self.messages.append(
                    f"ライブ端に追いつきました（飛ばしたセグメントは累計{self.skipped}）"
                )


def validate_media(media: MediaPlaylist) -> None:
    """窓の前提を検証する。空・4096超、target 0以下/60超、i-frames-only、各セグメントの
    duration 0以下/120超・URI 欠落・BYTERANGE・暗号化・不正 MAP、開始番号の負数・64bit 超を拒否。
    """
    if (
        not media.segments
        or len(media.segments) > MAX_ITEMS
        or media.target_duration <= 0
        or media.target_duration > 60
        or media.i_frames_only
        or any(
            segment.duration != segment.duration
            or segment.duration <= 0
            or segment.duration > 120
            or not segment.uri
            or segment.byte_range
            or segment.encrypted
            or segment.bad_map
            for segment in media.segments
        )
    ):
        raise ProbeError("playlist")
    if media.media_sequence < 0:
        raise ProbeError("playlist")
    if media.media_sequence + len(media.segments) > 2**64 - 1:
        raise ProbeError("playlist")


def segment_schedule(media: MediaPlaylist) -> SegmentSchedule:
    """EXTINF と PROGRAM-DATE-TIME を1パスで畳み込み、添字参照できる表にする。

    セグメント列と開始番号が同じ間は前回の表を使い回す。解析後のセグメントは
    不変であり、窓の更新は新しい `MediaPlaylist` を組み立てて行う。
    """
    schedule = getattr(media, "_schedule", None)
    base = getattr(media, "discontinuity_sequence", 0)
    segments = media.segments
    if (
        schedule is not None
        and schedule.segments is segments
        and schedule.media_sequence == media.media_sequence
        and schedule.base == base
    ):
        return schedule
    walls: list[datetime | None] = []
    discontinuities: list[int] = []
    wall = None
    count = 0
    for segment in segments:
        if segment.program_date:
            wall = _parse_program_date(segment.program_date)
        walls.append(wall)
        if segment.discontinuity:
            count += 1
        discontinuities.append(base + count)
        if wall is not None:
            wall = wall + timedelta(seconds=segment.duration)
    schedule = SegmentSchedule(segments, media.media_sequence, base, walls, discontinuities)
    media._schedule = schedule
    return schedule


def segment_wall(media: MediaPlaylist, sequence: int) -> datetime | None:
    """その通し番号の番組時刻。窓の外なら None。"""
    schedule = segment_schedule(media)
    offset = sequence - media.media_sequence
    if 0 <= offset < len(schedule.walls):
        return schedule.walls[offset]
    return None


def segment_discontinuity(media: MediaPlaylist, sequence: int) -> int | None:
    """開始番号を基準にした、その通し番号までの累積不連続番号。窓の外なら None。"""
    schedule = segment_schedule(media)
    offset = sequence - media.media_sequence
    if 0 <= offset < len(schedule.discontinuities):
        return schedule.discontinuities[offset]
    return None


def _parse_program_date(text: str) -> datetime:
    """PROGRAM-DATE-TIME の ISO 8601 を datetime にする。末尾の Z は UTC として扱い、
    不正な形式は ValueError。
    """
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def live_join_index(media: MediaPlaylist) -> int:
    """終端から target duration の3倍以内に始まる最も古いセグメント。"""
    horizon = float(media.target_duration) * 3.0
    from_end = 0.0
    index = max(len(media.segments) - 1, 0)
    for position, segment in reversed(list(enumerate(media.segments))):
        start_from_end = from_end + segment.duration
        if start_from_end > horizon and position + 1 < len(media.segments):
            break
        from_end = start_from_end
        index = position
        if start_from_end > horizon:
            break
    return index


def _forward_gap_is_reset(next_sequence: int, media: MediaPlaylist) -> bool:
    """next_sequence が新しい窓の手前へ窓長より大きく離れたら True。ライブ端へ再配置する。"""
    return media.media_sequence - next_sequence > len(media.segments)


def parse_media_playlist(text: str) -> MediaPlaylist:
    """メディアプレイリストを解析して検証する。1MB 上限、#EXTM3U、TARGETDURATION を必須にする。
    DISCONTINUITY と PROGRAM-DATE-TIME は次の1セグメント、MAP と KEY は以降へ引き継ぐ。
    """
    if len(text.encode()) > TEXT_LIMIT:
        raise ProbeError("large")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ProbeError("playlist")
    sequence = 0
    discontinuity_sequence = 0
    target = None
    end_list = False
    i_frames_only = False
    segments: list[Segment] = []
    duration = None
    discontinuity = False
    program_date = None
    map_uri = None
    map_bad = False
    encrypted = False
    byte_range = False
    for line in lines[1:]:
        try:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                sequence = int(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                discontinuity_sequence = int(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                target = int(line.split(":", 1)[1])
            elif line == "#EXT-X-ENDLIST":
                end_list = True
            elif line == "#EXT-X-I-FRAMES-ONLY":
                i_frames_only = True
            # DISCONTINUITY は次のセグメントに付く属性なので、ここではフラグだけ立てる。
            elif line == "#EXT-X-DISCONTINUITY":
                discontinuity = True
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                program_date = line.split(":", 1)[1]
            # MAP は次のセグメント以降に効く初期化セグメント。BYTERANGE 付きは未対応の印だけ付ける。
            elif line.startswith("#EXT-X-MAP:"):
                map_uri = parse_attribute(line, "URI")
                map_bad = not map_uri or "BYTERANGE=" in line
            elif line.startswith("#EXT-X-BYTERANGE:"):
                byte_range = True
            # KEY も次のセグメントから効く。METHOD が NONE なら平文に戻る。
            elif line.startswith("#EXT-X-KEY:"):
                method = parse_attribute(line, "METHOD")
                encrypted = method not in {None, "NONE"}
            elif line.startswith("#EXTINF:"):
                duration = float(line.split(":", 1)[1].split(",")[0])
            elif line.startswith("#"):
                continue
            else:
                if duration is None:
                    raise ProbeError("playlist")
                segments.append(
                    Segment(
                        line,
                        duration,
                        discontinuity,
                        program_date,
                        map_uri,
                        byte_range,
                        encrypted,
                        map_bad,
                    )
                )
                duration = None
                discontinuity = False
                program_date = None
                byte_range = False
        except ValueError as error:
            raise ProbeError("playlist") from error
    if target is None:
        raise ProbeError("playlist")
    media = MediaPlaylist(
        sequence,
        target,
        segments,
        end_list,
        i_frames_only=i_frames_only,
        discontinuity_sequence=discontinuity_sequence,
    )
    validate_media(media)
    return media


def parse_master_codec(text: str) -> str | None:
    """マスタープレイリストの最初の STREAM-INF から CODECS を返す。宣言が無ければ None。"""
    for line in text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:") and "CODECS=" in line:
            raw = parse_attribute(line, "CODECS")
            return raw.strip('"') if raw else None
    return None


def parse_attribute(line: str, name: str) -> str | None:
    """`KEY=value` または `KEY="value"` を1行から読む。"""
    marker = f"{name}="
    start = line.find(marker)
    if start < 0:
        return None
    rest = line[start + len(marker) :]
    # 引用符付きはカンマを含められるため、閉じ引用符までを値にする。
    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end]
    # 引用符なしは最初のカンマまで（属性の区切り）。
    return rest.split(",", 1)[0]
