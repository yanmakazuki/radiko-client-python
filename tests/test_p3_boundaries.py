"""P3（Issue #19）: 配信認証の一本化と共有型の境界。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import radiko.audio as audio
import radiko.coreaudio as coreaudio
import radiko.output as output
import radiko.prepare as prepare
from radiko.auth import authenticate, partial_key
from radiko.reauth import ReauthState
from radiko.storage import MemberSession
from radiko.stream import ProbeError


def test_prepare_authentication_goes_through_the_single_implementation(monkeypatch):
    seen = []

    def fake(fetch, session):
        seen.append(session.token)
        return {"x-radiko-authtoken": "shared-token"}, "JP13"

    monkeypatch.setattr("radiko.auth.authenticate", fake)
    headers, area = prepare.authenticate_stream(MemberSession("synthetic"), lambda: False)
    assert seen == ["synthetic"]
    assert headers == {"x-radiko-authtoken": "shared-token"}
    assert area == "JP13"


def test_prepare_authentication_adapter_keeps_the_production_urls(monkeypatch):
    calls = []

    def fake_get(url, headers=None, **kwargs):
        calls.append((url, dict(headers or {}), kwargs.get("limit")))
        if url.endswith("/v2/api/auth1"):
            return SimpleNamespace(
                headers={
                    "x-radiko-authtoken": "stream-token",
                    "x-radiko-keyoffset": "0",
                    "x-radiko-keylength": "4",
                },
                body=b"OK",
            )
        return SimpleNamespace(headers={}, body=b"JP13,Tokyo\n")

    monkeypatch.setattr(prepare, "stream_get", fake_get)
    headers, area = prepare.authenticate_stream(MemberSession("synthetic-token"), lambda: False)
    assert area == "JP13"
    assert headers == {"x-radiko-authtoken": "stream-token", "x-radiko-areaid": "JP13"}
    assert [url for url, _h, _l in calls] == [
        "https://api.radiko.jp/v2/api/auth1",
        "https://api.radiko.jp/v2/api/auth2",
    ]
    assert calls[0][1]["cookie"] == "radiko_session=synthetic-token"
    assert calls[1][1]["x-radiko-partialkey"] == partial_key(0, 4)
    assert all(limit == 64 * 1024 for _u, _h, limit in calls)
    assert "synthetic-token" not in str(headers)


def test_authenticate_maps_malformed_key_headers_to_auth_format():
    def fetch(path, _headers):
        return SimpleNamespace(
            headers={
                "x-radiko-authtoken": "token",
                "x-radiko-keyoffset": "not-a-number",
                "x-radiko-keylength": "4",
            },
            body=b"",
        )

    with pytest.raises(ProbeError) as raised:
        authenticate(fetch, MemberSession("token"))
    assert raised.value.code == "auth-format"


def test_authenticate_rejects_missing_tokens_and_bad_areas():
    def missing(_path, _headers):
        return SimpleNamespace(headers={}, body=b"JP13")

    with pytest.raises(ProbeError) as raised:
        authenticate(missing, MemberSession("token"))
    assert raised.value.code == "auth-format"

    def area(body):
        state = {"first": True}

        def fetch(_path, _headers):
            if state["first"]:
                state["first"] = False
                return SimpleNamespace(
                    headers={
                        "x-radiko-authtoken": "token",
                        "x-radiko-keyoffset": "0",
                        "x-radiko-keylength": "4",
                    },
                    body=b"",
                )
            return SimpleNamespace(headers={}, body=body)

        return fetch

    for body in (b"JP99,Tokyo\n", b"XX13,Tokyo\n", b"JP0\n", b"JP48\n", b"\xff\xfe"):
        with pytest.raises(ProbeError) as raised:
            authenticate(area(body), MemberSession("token"))
        expected = "auth-format" if body == b"\xff\xfe" else "outside-japan"
        assert raised.value.code == expected


def test_reauth_uses_the_injected_refresh():
    calls = []

    def refresh(session, _cancelled):
        calls.append(session.token)
        return {"x-radiko-authtoken": "new"}, "JP13"

    state = ReauthState(MemberSession("token"), lambda: False, refresh, now=lambda: 100.0)
    headers, how = state.renew({}, "HLS音声取得")
    assert how == "refreshed"
    assert calls == ["token"]
    assert headers == {"x-radiko-authtoken": "new"}
    # 20 秒の内側は同じトークンを使い、refresh は増えない。
    headers, how = state.renew(headers, "HLS音声取得")
    assert how == "reused"
    assert calls == ["token"]


def test_output_types_live_in_audio_and_are_reexported():
    assert output.OutputError is audio.OutputError
    assert output.OutputInfo is audio.OutputInfo
    assert output.OutputSummary is audio.OutputSummary
    assert coreaudio.OutputError is audio.OutputError
    assert coreaudio.OutputInfo is audio.OutputInfo
