"""HTTP 接続プール、stale 再接続、並列取得、gzip の実サーバテスト。"""

from __future__ import annotations

import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from radiko.catalog import CatalogError
from radiko.http_client import catalog_fetch, stream_get


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    accepted = 0

    def setup(self):
        super().setup()
        type(self).accepted += 1

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/gzip":
            body = gzip.compress(b"<radiko><stations/></radiko>")
            self._send(body, {"Content-Encoding": "gzip"})
        elif parsed.path == "/bomb":
            self._send(gzip.compress(b"x" * (4 * 1024 * 1024 + 1)), {"Content-Encoding": "gzip"})
        elif parsed.path == "/close":
            self.close_connection = True
            self._send(b"closed", {"Connection": "close"})
        else:
            value = parse_qs(parsed.query).get("i", ["ok"])[0]
            self._send(value.encode())

    def _send(self, body: bytes, headers: dict[str, str] | None = None):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _serve():
    _Handler.accepted = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_keep_alive_reuses_one_tcp_connection(monkeypatch):
    server = _serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("RADIKO_LOOPBACK_ORIGIN", base)
        first = stream_get(f"{base}/plain?i=one")
        second = stream_get(f"{base}/plain?i=two")
    finally:
        server.shutdown()
    assert first.body == b"one"
    assert second.body == b"two"
    assert _Handler.accepted == 1


def test_server_closed_connection_is_replaced_transparently(monkeypatch):
    server = _serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("RADIKO_LOOPBACK_ORIGIN", base)
        assert stream_get(f"{base}/close").body == b"closed"
        assert stream_get(f"{base}/plain?i=next").body == b"next"
    finally:
        server.shutdown()
    assert _Handler.accepted == 2


def test_four_parallel_fetches_do_not_mix_responses(monkeypatch):
    server = _serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("RADIKO_LOOPBACK_ORIGIN", base)
        results = {}
        errors = []

        def work(index):
            try:
                results[index] = stream_get(f"{base}/plain?i={index}").body
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        server.shutdown()
    assert not errors
    assert results == {index: str(index).encode() for index in range(4)}
    assert 1 <= _Handler.accepted <= 4


def test_catalog_fetch_decompresses_gzip_and_rejects_a_bomb():
    server = _serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert b"radiko" in catalog_fetch(f"{base}/gzip")
        with pytest.raises(CatalogError) as raised:
            catalog_fetch(f"{base}/bomb")
        assert raised.value.code == "large"
    finally:
        server.shutdown()
