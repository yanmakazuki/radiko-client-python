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
    """prepare.authenticate_stream が radiko.auth.authenticate の単一実装を呼ぶ。"""
    seen = []

    def fake(fetch, session):
        """受け取ったセッショントークンを記録して固定ヘッダーを返す。"""
        seen.append(session.token)
        return {"x-radiko-authtoken": "shared-token"}, "JP13"

    monkeypatch.setattr("radiko.auth.authenticate", fake)
    headers, area = prepare.authenticate_stream(MemberSession("synthetic"), lambda: False)
    assert seen == ["synthetic"]
    assert headers == {"x-radiko-authtoken": "shared-token"}
    assert area == "JP13"


def test_prepare_authentication_adapter_keeps_the_production_urls(monkeypatch):
    """authenticate_stream が本番の auth1 / auth2 URL・Cookie・partialkey・64KiB上限で通信する。"""
    calls = []

    def fake_get(url, headers=None, **kwargs):
        """auth1 と auth2 の呼び出し引数を記録し、擬似応答を返す。"""
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
    """auth1 の keyoffset が数値でない場合は ProbeError('auth-format') にする。"""

    def fetch(path, _headers):
        """壊れた keyoffset ヘッダーを持つ auth1 応答を返す。"""
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
    """auth1 のヘッダー欠落や auth2 の不正エリア・非UTF-8応答を適切なエラーコードで拒否する。"""

    def missing(_path, _headers):
        """必須ヘッダーの無い応答を返す。"""
        return SimpleNamespace(headers={}, body=b"JP13")

    with pytest.raises(ProbeError) as raised:
        authenticate(missing, MemberSession("token"))
    assert raised.value.code == "auth-format"

    def area(body):
        """1回目は正常な auth1、2回目は指定した body の auth2 を返す fetch を作る。"""
        state = {"first": True}

        def fetch(_path, _headers):
            """呼び出し順に応じて auth1 または auth2 の応答を返す。"""
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
    """ReauthState.renew が初回は注入された refresh を呼び、20秒以内はトークンを再利用する。"""
    calls = []

    def refresh(session, _cancelled):
        """セッショントークンを記録して新しい authtoken を返す。"""
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
    """音声出力の型は radiko.audio を実体とし、output と coreaudio から同じ型を参照できる。"""
    assert output.OutputError is audio.OutputError
    assert output.OutputInfo is audio.OutputInfo
    assert output.OutputSummary is audio.OutputSummary
    assert coreaudio.OutputError is audio.OutputError
    assert coreaudio.OutputInfo is audio.OutputInfo
