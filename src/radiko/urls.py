"""本番の配信先を組み立て、試験のときだけ 127.0.0.1 へ載せ替える。

経路そのものは `https://radiko.jp`、`api.radiko.jp`、`radiko-cf.com`、
`smartstream.ne.jp` のままである。`RADIKO_LOOPBACK_ORIGIN` は
`http://127.0.0.1:ポート` だけを許し、パスとクエリは変えない。
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from radiko.stream import ProbeError

_SUFFIXES = ("radiko.jp", "radiko-cf.com", "smartstream.ne.jp")


def trusted(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.username or parts.password:
        return False
    if parts.scheme != "https":
        return False
    if parts.port not in {None, 443}:
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _SUFFIXES)


def loopback_origin() -> str | None:
    value = os.environ.get("RADIKO_LOOPBACK_ORIGIN")
    if not value:
        return None
    parts = urlsplit(value)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or not parts.port
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or parts.username
    ):
        raise ProbeError("unsafe")
    return f"http://127.0.0.1:{parts.port}"


def transport_url(url: str) -> str:
    """本番 URL を検証してから、ループバックが設定されていればそこへ載せる。"""
    origin = loopback_origin()
    parts = urlsplit(url)
    if trusted(url):
        if origin is None:
            return url
        query = f"?{parts.query}" if parts.query else ""
        return f"{origin}{parts.path}{query}"
    if origin and parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port:
        return url
    raise ProbeError("unsafe")


def append_query(url: str, pairs: list[tuple[str, str]]) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(pairs)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
