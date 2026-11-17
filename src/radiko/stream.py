"""配信の時刻、局 ID、タイムフリー期限。プレイリストの実応答が聴取可否の最終判定である。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from radiko.domain import JST, broadcast_day, rfc3339
from radiko.storage import Membership


class ProbeError(Exception):
    """配信の検証・取得の失敗。`code` に種類、HTTP なら `status`、処理段階なら `stage`、
    包んだ元の失敗を `inner` に持つ。
    """

    def __init__(
        self,
        code: str,
        status: int | None = None,
        stage: str | None = None,
        inner: ProbeError | None = None,
    ):
        """失敗コードと、HTTP ステータス・段階・内側の例外を覚える。"""
        self.code = code
        self.status = status
        self.stage = stage
        self.inner = inner
        super().__init__(str(self))

    def at(self, stage: str) -> ProbeError:
        """この失敗を `stage` の文脈で包んだ新しい ProbeError("at") を返す。元は inner に残す。"""
        return ProbeError("at", stage=stage, inner=self)

    def __str__(self) -> str:
        """at は「段階: 内側」、それ以外は code に対応する日本語メッセージを返す。"""
        if self.code == "at" and self.inner is not None and self.stage is not None:
            return f"{self.stage}: {self.inner}"
        return _TEXT.get(self.code, "配信の検証中に通信に失敗しました").format(status=self.status)

    def __eq__(self, other: object) -> bool:
        """code・status・文字列表現が同じなら等しい。inner は文字列表現を通して比較される。"""
        return isinstance(other, ProbeError) and (self.code, self.status, str(self)) == (
            other.code,
            other.status,
            str(other),
        )


WINDOW_LOST = "通信が長く途切れたため、続きを再生できなくなりました。番組を選び直してください"
AUTH_EXPIRED = (
    "認証期限切れのため再生を停止しました。aで再確認するか、loginで再ログインしてください"
)
CONTRACT_REQUIRED = "対象の契約を確認できません（エリアフリーまたはタイムフリー30が必要です）"
GAVE_UP = "再接続の上限を超えたため再生を停止しました。番組を選び直して再生してください"

_TEXT = {
    "input": "局IDまたは日時が不正です（日時は日本時間のYYYYMMDDHHmmss）",
    "position": "シーク位置は番組冒頭以上・放送枠末尾未満で指定してください",
    "cancelled": "配信取得を取り消しました",
    "network": "配信の検証中に通信に失敗しました",
    "http": "配信サーバーがHTTP {status}を返しました",
    "authentication": "配信認証が拒否されました。sessionで会員認証を確認してください",
    "auth-expired": AUTH_EXPIRED,
    "denied": "配信サーバーがアクセスを拒否しました（契約・地域・配信条件の確認が必要です）",
    "not-found": "対象の番組または配信が見つかりません",
    "limited": "アクセス制限中です。時間をおいて再実行してください",
    "large": "応答または音声サンプルのサイズ上限を超えました",
    "unsafe": "未対応の配信先またはリダイレクトです",
    "auth-format": "配信認証の応答が想定形式と異なります",
    "outside-japan": "国内エリアを確認できませんでした",
    "xml": "局または配信設定XMLの形式が不正です",
    "program": "指定時刻と番組の放送枠を対応付けできません",
    "upcoming": "放送前のため配信取得できません",
    "on-air": "放送中のためタイムフリーの検証対象外です",
    "expired": "タイムフリーの候補期限を過ぎています",
    "contract": CONTRACT_REQUIRED,
    "outside": CONTRACT_REQUIRED,
    "unknown": "番組の配信条件が未確認のため取得を中止しました",
    "unavailable": "番組情報でタイムフリー配信対象外とされています",
    "playlist": "HLSプレイリストまたはサンプルの構造が不正・未対応です",
    "window": WINDOW_LOST,
    "endpoint": "対象モードと地域に対応する配信先がありません",
    "reconnect": GAVE_UP,
}


def parse_time(text: str) -> datetime:
    """YYYYMMDDHHmmss の14桁を JST の datetime にする。桁数・数字以外・日付不正は input。"""
    if len(text) != 14 or not text.isascii() or not text.isdigit():
        raise ProbeError("input")
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
        raise ProbeError("input") from error


def validate_station(station: str) -> None:
    """局IDを検証する。空・32文字超・ASCII 大文字/数字/-/_ 以外は input。"""
    if (
        not station
        or len(station) > 32
        or not all(
            char.isascii() and (char.isupper() or char.isdigit() or char in "-_")
            for char in station
        )
    ):
        raise ProbeError("input")


def morning_after_end_broadcast_day(end: datetime) -> datetime:
    """終了時刻の放送日の翌日 05:00 JST。7日・30日の期限そのものではない。"""
    day = broadcast_day(end)
    next_day = day + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, 5, 0, 0, tzinfo=JST)


@dataclass
class SlotProgram:
    """番組表の1枠。開始・終了、タイムフリー可否の4値、収録失敗、タイトルを持つ。"""

    start: datetime
    end: datetime
    timefree: tuple[bool | None, bool | None, bool | None, bool | None]
    failed_record: bool | None
    title: str | None = None


def eligible(program: SlotProgram, timefree: bool, now: datetime, membership: Membership) -> None:
    """再生時点の可否を判定する。放送前は upcoming、ライブは終了後 program、タイムフリーは
    放送中 on-air、期限切れ expired、無料期限後の未契約 contract。
    """
    if now < program.start:
        raise ProbeError("upcoming")
    if not timefree:
        if now < program.end:
            return
        raise ProbeError("program")
    if now < program.end:
        raise ProbeError("on-air")
    base = morning_after_end_broadcast_day(program.end)
    free_deadline = base + timedelta(days=7)
    paid_deadline = base + timedelta(days=30)
    if now >= paid_deadline:
        raise ProbeError("expired")
    if now >= free_deadline and membership.time_free_30 is not True:
        raise ProbeError("contract")


def area_contract(outside_area: bool, area_free: bool | None) -> ProbeError | None:
    """エリア外の契約判定。域内か area_free=True なら None、契約なし False は outside、
    未確認 None は contract。
    """
    if not outside_area or area_free is True:
        return None
    if area_free is False:
        return ProbeError("outside")
    return ProbeError("contract")


def availability(program: SlotProgram, now: datetime, outside: bool) -> None:
    """番組情報のタイムフリー可否を見る。failed_record が True なら unavailable、None なら unknown。
    timefree は域内/域外と7日期限の前後を添字にし、True は可、False は unavailable、None は unknown。
    """
    if program.failed_record is True:
        raise ProbeError("unavailable")
    if program.failed_record is None:
        raise ProbeError("unknown")
    base = morning_after_end_broadcast_day(program.end)
    tsplus = base + timedelta(days=7)
    index = int(outside) * 2 + int(now >= tsplus)
    flag = program.timefree[index]
    if flag is True:
        return
    if flag is False:
        raise ProbeError("unavailable")
    raise ProbeError("unknown")


def deadline_pair(end: datetime) -> tuple[str, str]:
    """無料期限（放送日の翌日05:00+7日）と有料期限（+30日）を RFC3339 で返す。"""
    base = morning_after_end_broadcast_day(end)
    return rfc3339(base + timedelta(days=7)), rfc3339(base + timedelta(days=30))
