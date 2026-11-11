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
    def __init__(self, session: MemberSession, cancelled, refresh, now=None):
        self.session = session
        self.cancelled = cancelled
        self.refresh = refresh
        self.now = now or time.monotonic
        self.last: float | None = None
        self.count = 0
        self.episode = False

    def renew(self, headers: dict[str, str], stage: str) -> tuple[dict[str, str], str]:
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
