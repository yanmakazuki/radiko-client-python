"""制限付き HTTP。取り消した応答の本文は返さない。ログに URL 以外の秘密を残さない。

GET は http.client の接続プールを共有し、同じオリジンへの連続取得で TCP/TLS を
再利用する。壊れた古い接続は 1 回だけ新規接続でやり直す。POST は再試行しない。
"""

from __future__ import annotations

import contextvars
import http.client
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlencode, urljoin, urlparse

from radiko.catalog import CatalogError, decode_body
from radiko.stream import ProbeError
from radiko.urls import transport_url, trusted


class Reply:
    """HTTP 応答。ヘッダー名は小文字に揃え、`url` はリダイレクト後の最終 URL。"""

    def __init__(self, status: int, body: bytes, headers: dict[str, str], url: str) -> None:
        """ステータス・本文・ヘッダー・最終 URL を保持する。ヘッダー名は小文字にする。"""
        self.status = status
        self.body = body
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.url = url


def _allowed(url: str) -> None:
    """URL の送信先を検証する。認証情報付き URL は拒否し、http は 127.0.0.1 の明示ポートのみ、
    https は radiko.jp / radiko-cf.com / smartstream.ne.jp とそのサブドメインだけを許可する。
    """
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise ProbeError("unsafe")
    if parsed.scheme == "http" and (parsed.hostname or "") == "127.0.0.1" and parsed.port:
        return
    if trusted(url):
        return
    raise ProbeError("unsafe")


class _StaleConnection(Exception):
    """接続再利用時の切断を表す内部例外。現行実装では送出せず、切断は http.client の例外から
    ProbeError("network") へ写す。
    """

    pass


class _ConnectionPool:
    """(scheme, host, port) ごとのアイドル接続。"""

    def __init__(self) -> None:
        """アイドル接続の辞書とロックを用意する。キーごとに最大4本まで保持する。"""
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}
        self.max_idle = 4

    def acquire(self, key, timeout: float, fresh: bool = False):
        """キーの接続を取り出す。fresh でなければアイドルを LIFO で再利用し、無ければ新規に作る。
        再利用時は timeout を現在の値へ更新する。
        """
        if not fresh:
            with self._lock:
                connections = self._idle.get(key)
                connection = connections.pop() if connections else None
            if connection is not None:
                connection.timeout = timeout
                if connection.sock is not None:
                    connection.sock.settimeout(timeout)
                return connection
        scheme, host, port = key
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def release(self, key, connection) -> None:
        """接続をアイドルへ戻す。既に4本あるキーでは close して捨てる。"""
        with self._lock:
            connections = self._idle.setdefault(key, [])
            if len(connections) >= self.max_idle:
                keep = False
            else:
                connections.append(connection)
                keep = True
        if not keep:
            connection.close()

    def discard(self, connection) -> None:
        """接続を閉じる。close 自体の OSError は無視する。"""
        try:
            connection.close()
        except OSError:
            pass


_pool = _ConnectionPool()


def _key(url: str):
    """接続プールのキー (scheme, host, port) を返す。ポート省略時は https=443、http=80。"""
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname or "", port


def _path(url: str) -> str:
    """リクエスト行に使うパスを返す。空なら "/"、クエリはそのまま付ける。"""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def get(
    url: str,
    headers: dict[str, str] | None = None,
    limit: int = 1024 * 1024,
    timeout: float = 20.0,
    cancelled=None,
    retries: int = 0,
) -> Reply:
    """GET。retries は通信断・408・5xx のやり直し回数。"""
    return _request(
        "GET",
        url,
        headers or {},
        None,
        limit,
        timeout,
        cancelled,
        redirects=True,
        retries=retries,
        use_transport=False,
        strict=False,
    )


def _retry_delay_ms(retries: int) -> float:
    """Rust の 100ms * 2^n に 0〜49ms のジッタを足す。"""
    return 100 * (1 << retries) + (time.time_ns() // 1_000_000 % 50)


def _request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    limit: int,
    timeout: float,
    cancelled,
    redirects: bool,
    retries: int,
    use_transport: bool,
    strict: bool,
    byte_range: tuple[int, int] | None = None,
    record_delays: bool = False,
) -> Reply:
    """共通のリクエスト。許可ホストを検証し、通信断と 408/5xx を retries 回までやり直す。
    strict なら _raise_for_status で検査し、返す Reply.url は最終 URL。
    """
    cancelled = cancelled or (lambda: False)
    current = transport_url(url) if use_transport else url
    _allowed(current)
    attempt = 0
    while True:
        try:
            status, header_map, payload, final = _exchange_chain(
                method,
                current,
                headers,
                body,
                limit,
                timeout,
                cancelled,
                redirects,
                use_transport,
                byte_range,
                retry_stale=method != "POST",
                record_delays=record_delays,
            )
        except ProbeError as error:
            # 通信断は再試行。キャンセル済みなら待たずに送出する。
            if _retryable(error) and attempt < retries and not cancelled():
                _backoff(attempt, cancelled, record_delays)
                attempt += 1
                continue
            raise
        if strict:
            try:
                _raise_for_status(status, byte_range is not None)
            except ProbeError as error:
                if _retryable(error) and attempt < retries and not cancelled():
                    _backoff(attempt, cancelled, record_delays)
                    attempt += 1
                    continue
                raise
        # 408 と 5xx は同じ URL でやり直す。408 を除く 4xx は再試行しても結果が変わらない。
        elif (status == 408 or 500 <= status <= 599) and attempt < retries and not cancelled():
            _backoff(attempt, cancelled, record_delays)
            attempt += 1
            continue
        return Reply(status, payload, header_map, final)


def _retryable(error: ProbeError) -> bool:
    """やり直してよい ProbeError か。network と http の 408/5xx を対象にする。"""
    return error.is_transient_http()


def _backoff(attempt: int, cancelled, record: bool) -> None:
    """指数バックオフで待つ。record なら待ち時間を contextvar に記録する。"""
    delay = _retry_delay_ms(attempt) / 1000
    if record:
        recorded = _transport_delays.get()
        if recorded is not None:
            recorded.append(delay)
    _retry_wait(delay, cancelled)


def _exchange_chain(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    limit: int,
    timeout: float,
    cancelled,
    redirects: bool,
    use_transport: bool,
    byte_range: tuple[int, int] | None,
    retry_stale: bool,
    record_delays: bool,
) -> tuple[int, dict[str, str], bytes, str]:
    """リダイレクトを追って1回の交換を完了する。301/302/303/307/308 を最大4回まで追い、
    POST の 301/302/303 は GET に変える。各遷移先を再検証し、Range は引き継がない。
    """
    current = url
    hops = 0
    while True:
        if cancelled():
            raise ProbeError("cancelled")
        status, header_map, payload = _exchange(
            method,
            current,
            headers,
            body,
            limit,
            timeout,
            cancelled,
            byte_range,
            retry_stale,
            record_delays,
        )
        if status in {301, 302, 303, 307, 308} and redirects:
            if hops >= 4:
                raise ProbeError("unsafe")
            location = header_map.get("location")
            if not location:
                raise ProbeError("unsafe")
            target = urljoin(current, location)
            current = transport_url(target) if use_transport else target
            _allowed(current)
            hops += 1
            byte_range = None
            if method == "POST" and status in {301, 302, 303}:
                method, body = "GET", None
            continue
        return status, header_map, payload, current


def _exchange(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    limit: int,
    timeout: float,
    cancelled,
    byte_range: tuple[int, int] | None,
    retry_stale: bool,
    record_delays: bool,
) -> tuple[int, dict[str, str], bytes]:
    # GET だけ接続の使い回しで切れた場合に1回やり直す。POST は二重送信を避けて再送しない。
    """1 URL の交換。GET は stale 接続なら1回だけ接続を取り直して再送し、POST は二重送信を避けて
    再送しない（常に新規接続）。応答は上限まで読み、再利用可能ならプールへ返す。
    """
    attempts = 2 if retry_stale else 1
    for attempt in range(attempts):
        key = _key(url)
        connection = _pool.acquire(key, timeout, fresh=method == "POST")
        request_headers = {name: value for name, value in headers.items()}
        if byte_range is not None:
            offset, length = byte_range
            if length <= 0 or length > limit:
                _pool.discard(connection)
                raise ProbeError("large")
            request_headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        try:
            if cancelled():
                raise ProbeError("cancelled")
            connection.request(method, _path(url), body=body, headers=request_headers)
            response = connection.getresponse()
            status = response.status
            header_map = {name.lower(): value for name, value in response.getheaders()}
            payload = _read_limited(response, limit, cancelled)
            reusable = _reusable(response, header_map)
            if reusable:
                _pool.release(key, connection)
            else:
                _pool.discard(connection)
            return status, header_map, payload
        except ProbeError:
            _pool.discard(connection)
            raise
        except (
            http.client.RemoteDisconnected,
            http.client.BadStatusLine,
            http.client.IncompleteRead,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
        ) as error:
            _pool.discard(connection)
            if attempt + 1 < attempts and not cancelled():
                _backoff(0, cancelled, record_delays)
                continue
            raise ProbeError("network") from error
        except (
            http.client.HTTPException,
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            OSError,
        ) as error:
            _pool.discard(connection)
            raise ProbeError("network") from error
    raise ProbeError("network")


def _reusable(response, headers: dict[str, str]) -> bool:
    """応答後に接続をプールへ戻せるか。Connection: close か will_close なら戻さない。"""
    if response.will_close:
        return False
    if headers.get("connection", "").lower() == "close":
        return False
    return True


def _read_limited(response, limit: int, cancelled) -> bytes:
    """本文を最大 limit バイトまで読む。超えたら ProbeError("large")、キャンセルは cancelled。"""
    chunks = []
    total = 0
    while total <= limit:
        if cancelled():
            raise ProbeError("cancelled")
        chunk = response.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        chunks.append(chunk)
    body = b"".join(chunks)
    if len(body) > limit:
        raise ProbeError("large")
    return body


def post_form(
    url: str, fields: dict[str, str], headers: dict[str, str] | None = None, limit: int = 64 * 1024
) -> Reply:
    """ログイン POST。再試行しない。fields は呼び出し後に捨てる前提。"""
    _allowed(url)
    data = urlencode(fields).encode()
    request_headers = {name: value for name, value in (headers or {}).items()}
    request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    return _request(
        "POST",
        url,
        request_headers,
        data,
        limit,
        20.0,
        None,
        redirects=False,
        retries=0,
        use_transport=False,
        strict=False,
    )


def catalog_fetch(url: str) -> bytes:
    """番組表を取得して gzip を解凍する。4MB 上限、通信は最大3回再試行し、
    失敗は CatalogError（cancelled / network / large / limited / http）に写す。
    """
    try:
        reply = get(url, {"accept-encoding": "gzip, identity"}, limit=4 * 1024 * 1024, retries=3)
    except ProbeError as error:
        if error.code == "cancelled":
            raise CatalogError("cancelled") from error
        if error.code == "network":
            raise CatalogError("network") from error
        if error.code == "large":
            raise CatalogError("large") from error
        if error.code == "http":
            if error.status == 429:
                raise CatalogError("limited") from error
            raise CatalogError("http", status=error.status) from error
        raise CatalogError("network") from error
    if reply.status == 429:
        raise CatalogError("limited")
    if reply.status != 200:
        raise CatalogError("http", status=reply.status)
    encoding = reply.headers.get("content-encoding")
    return decode_body(reply.body, encoding)


_transport_sleep = contextvars.ContextVar("radiko_transport_sleep", default=None)
_transport_delays = contextvars.ContextVar("radiko_transport_delays", default=None)


@contextmanager
def transport_overrides(sleep, delays):
    """再接続の内側の待ちだけを差し替える。認証の POST には使わない。"""
    sleep_token = _transport_sleep.set(sleep)
    delay_token = _transport_delays.set(delays)
    try:
        yield
    finally:
        _transport_sleep.reset(sleep_token)
        _transport_delays.reset(delay_token)


def _retry_wait(delay: float, cancelled) -> None:
    """100ms から倍々の待ちを 10ms ごとに取り消し確認する。ジッタは呼び出し側で足す。"""
    sleep = _transport_sleep.get() or time.sleep
    if cancelled():
        raise ProbeError("cancelled")
    left = delay
    while left > 1e-12:
        if cancelled():
            raise ProbeError("cancelled")
        slice_ = min(0.01, left)
        sleep(slice_)
        left -= slice_


def stream_get(
    url: str,
    headers: dict[str, str] | None = None,
    limit: int = 1024 * 1024,
    timeout: float = 20.0,
    cancelled=None,
    redirects: bool = False,
    byte_range: tuple[int, int] | None = None,
    readonly_retry: bool = False,
) -> Reply:
    """本番 URL を載せ替えて取得する。401/403/404/429 は配信エラーにする。

    readonly_retry は再生の GET だけが使う。失敗 1 回につき 100ms、200ms、400ms に
    0〜49ms を足して最大 3 回やり直す。認証の request_once には渡さない。
    """
    return _request(
        "GET",
        url,
        headers or {},
        None,
        limit,
        timeout,
        cancelled,
        redirects=redirects,
        retries=3 if readonly_retry else 0,
        use_transport=True,
        strict=True,
        byte_range=byte_range,
        record_delays=True,
    )


def _raise_for_status(status: int, ranged: bool) -> None:
    """配信 GET のステータスを検査する。200（非 Range）と 206（Range）以外は
    401 authentication / 403 denied / 404 not-found / 429 limited / その他 http。
    """
    if status == 200 and not ranged:
        return
    if status == 206 and ranged:
        return
    if status == 401:
        raise ProbeError("authentication")
    if status == 403:
        raise ProbeError("denied")
    if status == 404:
        raise ProbeError("not-found")
    if status == 429:
        raise ProbeError("limited")
    raise ProbeError("http", status=status)
