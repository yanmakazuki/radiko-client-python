"""会員ログインと契約フラグ。パスワードと応答本文はエラーへ含めない。"""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlparse

from radiko.http_client import get, post_form
from radiko.storage import MemberSession, Membership
from radiko.stream import ProbeError
from radiko.urls import loopback_origin

MAX_RESPONSE = 64 * 1024
LOGIN_PATH = "/ap/member/webapi/member/login"
CHECK_PATH = "/ap/member/webapi/v2/member/login/check"


class AuthError(Exception):
    """認証の失敗。`code` で種類を、HTTP 由来なら `status` を持つ。
    利用者向けメッセージにパスワードや応答本文は含めない。
    """

    def __init__(self, code: str, status: int | None = None) -> None:
        """失敗コードと HTTP ステータスを覚える。`status` は HTTP 由来の失敗だけに入る。"""
        self.code = code
        self.status = status
        super().__init__(str(self))

    def __str__(self) -> str:
        """code に対応する日本語メッセージを返す。未知の code は通信エラーとして扱う。"""
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
        """code と status が同じなら等しい。メッセージや引数は比較しない。"""
        return isinstance(other, AuthError) and (self.code, self.status) == (
            other.code,
            other.status,
        )


def auth_base() -> str:
    """認証 API のベース URL を返す。RADIKO_AUTH_ORIGIN は 127.0.0.1 の http だけ許可し、
    それ以外は AuthError("network")。未指定なら loopback_origin()、最後に https://radiko.jp。
    """
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
    origin = loopback_origin()
    if origin:
        return origin
    return "https://radiko.jp"


class MemberClient:
    """会員ログインとセッション確認。通信・待ち・取り消し判定は差し替えられる。"""

    def __init__(self, transport=None, sleep=None, cancelled=None) -> None:
        """通信・待ち・取り消し判定を差し込む。省略時は実 HTTP、待たない、取り消さない。"""
        self.transport = transport
        self.sleep = sleep or (lambda _seconds: None)
        self.cancelled = cancelled or (lambda: False)

    @classmethod
    def create(cls, cancelled=None) -> MemberClient:
        """取り消し済みでなければクライアントを作る。取り消し済みなら AuthError("cancelled")。"""
        if cancelled and cancelled():
            raise AuthError("cancelled")
        return cls(cancelled=cancelled)

    def login(self, mail: str, password: str) -> tuple[MemberSession, Membership]:
        """ログインしてセッションと会員情報を返す。空入力は input、401 は credentials、
        セッションを読めない応答は format。成功後は check で会員情報を取る。
        """
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
        """保存セッションを確認して会員情報を返す。408/5xx と通信断は最大3回再試行し、
        400/401 は expired、その他は _success の規則に従う。待ちは 0.1/0.2/0.4 秒。
        """
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
        """ログイン POST を送る。transport があれば優先し、ProbeError は AuthError に写す。"""
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
        """セッション確認 GET を送る。transport を優先し、HTTP の再試行はせず ProbeError を写す。"""
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
    """`_login` / `_check` が共通して扱う最小の応答。`status` と `body` だけを持つ。"""

    def __init__(self, status: int, body: bytes) -> None:
        """ステータスと本文をそのまま保持する。"""
        self.status = status
        self.body = body


def _success(status: int) -> None:
    """200 以外を失敗にする。429 は limited、それ以外は http(status)。"""
    if status == 200:
        return
    if status == 429:
        raise AuthError("limited")
    raise AuthError("http", status)


def parse_membership(data: bytes) -> Membership:
    """会員情報の JSON を解析する。status が 200 なら areafree/timefreeplus を真偽値にし、
    400/401 は expired、それ以外や壊れた JSON は format。
    """
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
    """会員フラグを True/False/None に揃える。"1"/1 は True、"0"/0 は False、他は format。"""
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
    """HTTP 層の ProbeError を認証用の AuthError に写す。cancelled と large だけ区別する。"""
    if error.code == "cancelled":
        return AuthError("cancelled")
    if error.code == "large":
        return AuthError("large")
    return AuthError("network")


PLAYER_KEY = b"bcd151073c03b352e1ef2fd66c32209da9ca0afa"


def partial_key(offset: int, length: int) -> str:
    """auth1 が示した offset/length で PLAYER_KEY を切り出し、Base64 にする。
    範囲外や長さ0以下は ProbeError("auth-format")。
    """
    end = offset + length
    if offset < 0 or length <= 0 or end > len(PLAYER_KEY):
        raise ProbeError("auth-format")
    return base64.b64encode(PLAYER_KEY[offset:end]).decode()


def authenticate(fetch, session: MemberSession) -> tuple[dict[str, str], str]:
    """配信認証の唯一の実装。返したヘッダーはログに出さない。"""
    headers = {
        "x-radiko-app": "pc_html5",
        "x-radiko-app-version": "0.0.1",
        "x-radiko-device": "pc",
        "x-radiko-user": "dummy_user",
        "cookie": f"radiko_session={session.token}",
    }
    first = fetch("/v2/api/auth1", headers)
    token = _header(first, "x-radiko-authtoken")
    try:
        offset = int(_header(first, "x-radiko-keyoffset"))
        length = int(_header(first, "x-radiko-keylength"))
    except ValueError as error:
        raise ProbeError("auth-format") from error
    headers = {
        **headers,
        "x-radiko-authtoken": token,
        "x-radiko-partialkey": partial_key(offset, length),
        "x-radiko-session": session.token,
        "x-radiko-connection": "wifi",
    }
    second = fetch("/v2/api/auth2", headers)
    try:
        area_raw = second.body.decode().strip().split(",")[0]
    except UnicodeError as error:
        raise ProbeError("auth-format") from error
    if not area_raw.startswith("JP"):
        raise ProbeError("outside-japan")
    try:
        number = int(area_raw[2:])
    except ValueError as error:
        raise ProbeError("outside-japan") from error
    if not 1 <= number <= 47:
        raise ProbeError("outside-japan")
    area = f"JP{number}"
    safe = {"x-radiko-authtoken": token, "x-radiko-areaid": area}
    return safe, area


def _header(reply, name: str) -> str:
    """応答ヘッダーを大文字小文字を問わず引く。無ければ ProbeError("auth-format")。"""
    value = reply.headers.get(name.lower()) or reply.headers.get(name)
    if not value:
        raise ProbeError("auth-format")
    return value
