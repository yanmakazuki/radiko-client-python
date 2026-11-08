"""制限付き HTTP。取り消した応答の本文は返さない。ログに URL 以外の秘密を残さない。"""

from __future__ import annotations

import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from radiko.catalog import CatalogError, decode_body
from radiko.stream import ProbeError


class Reply:
    def __init__(self, status: int, body: bytes, headers: dict[str, str], url: str) -> None:
        self.status = status
        self.body = body
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.url = url


def _allowed(url: str) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise ProbeError("unsafe")
    host = parsed.hostname or ""
    if parsed.scheme == "http" and host == "127.0.0.1" and parsed.port:
        return
    if parsed.scheme == "https" and host.endswith(("radiko.jp", "radiko-cf.com")):
        return
    raise ProbeError("unsafe")


def get(
    url: str,
    headers: dict[str, str] | None = None,
    limit: int = 1024 * 1024,
    timeout: float = 20.0,
    cancelled=None,
    retries: int = 0,
) -> Reply:
    """GET。retries は通信断・408・5xx のやり直し回数。"""
    _allowed(url)
    cancelled = cancelled or (lambda: False)
    attempt = 0
    while True:
        if cancelled():
            raise ProbeError("cancelled")
        try:
            reply = _once(url, headers or {}, limit, timeout, cancelled)
        except ProbeError as error:
            if error.code in {"network", "http"} and (
                error.code == "network" or error.status == 408 or (error.status or 0) >= 500
            ):
                if attempt < retries and not cancelled():
                    attempt += 1
                    time.sleep(min(0.05 * attempt, 0.2))
                    continue
            raise
        if reply.status == 408 or 500 <= reply.status <= 599:
            if attempt < retries and not cancelled():
                attempt += 1
                time.sleep(min(0.05 * attempt, 0.2))
                continue
        return reply


def _once(url: str, headers: dict[str, str], limit: int, timeout: float, cancelled) -> Reply:
    if cancelled():
        raise ProbeError("cancelled")
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            header_map = dict(response.headers.items())
            body = _read_limited(response, limit, cancelled)
            final = response.geturl()
    except HTTPError as error:
        status = error.code
        header_map = dict(error.headers.items()) if error.headers else {}
        try:
            body = error.read(limit + 1)
        except Exception as read_error:  # noqa: BLE001
            raise ProbeError("network") from read_error
        if len(body) > limit:
            raise ProbeError("large")
        final = url
    except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as error:
        raise ProbeError("network") from error
    if cancelled():
        raise ProbeError("cancelled")
    _allowed(final)
    return Reply(status, body, header_map, final)


def _read_limited(response, limit: int, cancelled) -> bytes:
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
    from urllib.parse import urlencode

    data = urlencode(fields).encode()
    request = Request(url, data=data, headers=headers or {}, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read(limit + 1)
            status = response.status
            header_map = dict(response.headers.items())
    except HTTPError as error:
        status = error.code
        header_map = dict(error.headers.items()) if error.headers else {}
        body = error.read(limit + 1)
    except (URLError, TimeoutError, OSError) as error:
        raise ProbeError("network") from error
    if len(body) > limit:
        raise ProbeError("large")
    return Reply(status, body, header_map, url)


def catalog_fetch(url: str) -> bytes:
    try:
        reply = get(url, {"accept-encoding": "identity"}, limit=4 * 1024 * 1024, retries=3)
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
