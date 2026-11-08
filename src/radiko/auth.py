"""会員ログインと契約フラグ。パスワードと応答本文はエラーへ含めない。"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from radiko.http_client import get, post_form
from radiko.storage import MemberSession, Membership
from radiko.stream import ProbeError

MAX_RESPONSE = 64 * 1024
LOGIN_PATH = "/ap/member/webapi/member/login"
CHECK_PATH = "/ap/member/webapi/v2/member/login/check"


class AuthError(Exception):
    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.code == "credentials":
            return "ログインに失敗しました。メールアドレスとパスワードを確認してください"
        if self.code == "expired":
            return "セッションが失効しました。loginで再ログインしてください"
        if self.code == "cancelled":
            return "認証処理を取り消しました"
        if self.code == "network":
            return "認証サーバーと通信できません"
        if self.code == "http":
            return f"認証サーバーがHTTP {self.status}を返しました"
        if self.code == "limited":
            return "認証サーバーのアクセス制限中です。時間をおいて再実行してください"
        if self.code == "format":
            return "認証応答が想定形式と異なります"
        if self.code == "large":
            return "認証応答のサイズ上限を超えました"
        if self.code == "input":
            return "メールアドレスまたはパスワードが空です"
        return "認証サーバーと通信できません"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AuthError) and (self.code, self.status) == (
            other.code,
            other.status,
        )


def auth_base() -> str:
    override = os.environ.get("RADIKO_AUTH_ORIGIN")
    if override:
        parsed = urlparse(override)
        if (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port
            and parsed.path in {"", "/"}
        ):
            return f"http://127.0.0.1:{parsed.port}"
        raise AuthError("network")
    from radiko.urls import loopback_origin

    origin = loopback_origin()
    if origin:
        return origin
    return "https://radiko.jp"


class MemberClient:
    def __init__(self, transport=None, sleep=None, cancelled=None) -> None:
        self.transport = transport
        self.sleep = sleep or (lambda _seconds: None)
        self.cancelled = cancelled or (lambda: False)

    @classmethod
    def create(cls, cancelled=None) -> MemberClient:
        if cancelled and cancelled():
            raise AuthError("cancelled")
        return cls(cancelled=cancelled)

    def login(self, mail: str, password: str) -> tuple[MemberSession, Membership]:
        if self.cancelled():
            raise AuthError("cancelled")
        if not mail.strip() or password == "":
            raise AuthError("input")
        response = self._login(mail, password)
        if response.status == 401:
            raise AuthError("credentials")
        _success(response.status)
        try:
            value = json.loads(response.body.decode())
            token = value["radiko_session"]
            session = MemberSession(token)
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise AuthError("format") from error
        membership = self.check(session)
        return session, membership

    def check(self, session: MemberSession) -> Membership:
        retries = 0
        while True:
            if self.cancelled():
                raise AuthError("cancelled")
            try:
                response = self._check(session)
                retry = response.status == 408 or 500 <= response.status <= 599
                error = None
            except AuthError as caught:
                response = None
                retry = caught.code == "network"
                error = caught
            if retry and retries < 3:
                self.sleep(0.1 * (1 << retries))
                retries += 1
                continue
            if error is not None:
                raise error
            assert response is not None
            break
        if response.status in {400, 401}:
            raise AuthError("expired")
        _success(response.status)
        return parse_membership(response.body)

    def _login(self, mail: str, password: str):
        if self.transport is not None:
            return self.transport.login(mail, password)
        try:
            reply = post_form(
                f"{auth_base()}{LOGIN_PATH}", {"mail": mail, "pass": password}, limit=MAX_RESPONSE
            )
        except ProbeError as error:
            raise _from_probe(error) from error
        return _HttpResponse(reply.status, reply.body)

    def _check(self, session: MemberSession):
        if self.transport is not None:
            return self.transport.check(session)
        try:
            reply = get(
                f"{auth_base()}{CHECK_PATH}",
                {"cookie": f"radiko_session={session.token}"},
                limit=MAX_RESPONSE,
                retries=0,
                cancelled=self.cancelled,
            )
        except ProbeError as error:
            raise _from_probe(error) from error
        return _HttpResponse(reply.status, reply.body)


class _HttpResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body


def _success(status: int) -> None:
    if status == 200:
        return
    if status == 429:
        raise AuthError("limited")
    raise AuthError("http", status)


def parse_membership(data: bytes) -> Membership:
    try:
        value = json.loads(data.decode())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AuthError("format") from error
    status = value.get("status") if isinstance(value, dict) else None
    if status in {"200", 200}:
        pass
    elif status in {"400", "401", 400, 401}:
        raise AuthError("expired")
    else:
        raise AuthError("format")
    return Membership(_flag(value.get("areafree")), _flag(value.get("timefreeplus")))


def _flag(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if value in {"1", 1}:
        return True
    if value in {"0", 0}:
        return False
    raise AuthError("format")


def _from_probe(error: ProbeError) -> AuthError:
    if error.code == "cancelled":
        return AuthError("cancelled")
    if error.code == "large":
        return AuthError("large")
    return AuthError("network")


PLAYER_KEY = b"bcd151073c03b352e1ef2fd66c32209da9ca0afa"


def partial_key(offset: int, length: int) -> str:
    import base64

    end = offset + length
    if offset < 0 or length <= 0 or end > len(PLAYER_KEY):
        raise ProbeError("auth-format")
    return base64.b64encode(PLAYER_KEY[offset:end]).decode()


def authenticate(fetch, session: MemberSession) -> tuple[dict[str, str], str]:
    """配信認証。返したヘッダーはログに出さない。"""
    headers = {
        "x-radiko-app": "pc_html5",
        "x-radiko-app-version": "0.0.1",
        "x-radiko-device": "pc",
        "x-radiko-user": "dummy_user",
        "cookie": f"radiko_session={session.token}",
    }
    first = fetch("/v2/api/auth1", headers)
    token = _header(first, "x-radiko-authtoken")
    offset = int(_header(first, "x-radiko-keyoffset"))
    length = int(_header(first, "x-radiko-keylength"))
    headers = {
        **headers,
        "x-radiko-authtoken": token,
        "x-radiko-partialkey": partial_key(offset, length),
        "x-radiko-session": session.token,
        "x-radiko-connection": "wifi",
    }
    second = fetch("/v2/api/auth2", headers)
    area_raw = second.body.decode().strip().split(",")[0]
    if not area_raw.startswith("JP"):
        raise ProbeError("outside-japan")
    number = int(area_raw[2:])
    if not 1 <= number <= 47:
        raise ProbeError("outside-japan")
    area = f"JP{number}"
    safe = {"x-radiko-authtoken": token, "x-radiko-areaid": area}
    return safe, area


def _header(reply, name: str) -> str:
    value = reply.headers.get(name.lower()) or reply.headers.get(name)
    if not value:
        raise ProbeError("auth-format")
    return value
