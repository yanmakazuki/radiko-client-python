"""再生中の HTTP 401 だけを、保存済みセッションで一度取り直す。

成功した再認証から 20 秒は authenticate を呼ばず、持っているトークンで 1 回だけ取り直す。
403 と 429 は再認証しない。取り消した再認証は期限切れにしない。
"""

from __future__ import annotations

import time

from radiko.storage import MemberSession
from radiko.stream import ProbeError

REAUTH_INTERVAL = 20.0


class ReauthState:
    """再生中の401をエピソードごとに1回だけ再認証する状態。20秒以内はトークンを再利用し、403/429は対象外。"""

    def __init__(self, session: MemberSession, cancelled, refresh, now=None):
        """保存セッション・取り消し判定・refresh 関数・時計を保持する。last は最後の再認証を始めた時刻。"""
        self.session = session
        self.cancelled = cancelled
        self.refresh = refresh
        self.now = now or time.monotonic
        self.last: float | None = None
        self.count = 0
        self.episode = False

    def renew(self, headers: dict[str, str], stage: str) -> tuple[dict[str, str], str]:
        """headers を更新する。直近20秒または同じ401の連続なら既存の authtoken を再利用し、
        無ければ refresh を1回呼ぶ。
        """
        recent = self.last is not None and self.now() - self.last < REAUTH_INTERVAL
        if self.episode or recent:
            if "x-radiko-authtoken" in headers:
                return headers, "reused"
            raise ProbeError("auth-expired").at(stage)
        self.episode = True
        self.last = self.now()
        try:
            refreshed, _area = self.refresh(self.session, self.cancelled)
        except ProbeError as error:
            if error.code == "cancelled":
                raise ProbeError("cancelled").at(stage) from error
            if error.code == "limited":
                raise ProbeError("limited").at(stage) from error
            if error.code == "network":
                raise ProbeError("network").at(stage) from error
            if error.code == "denied":
                raise ProbeError("denied").at(stage) from error
            if error.code == "at":
                raise
            raise ProbeError("auth-expired").at(stage) from error
        if "x-radiko-authtoken" not in refreshed:
            raise ProbeError("auth-expired").at(stage)
        self.count += 1
        return refreshed, "refreshed"
