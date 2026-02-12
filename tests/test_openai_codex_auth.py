import asyncio
import base64
import json
import time
from pathlib import Path

import pytest

from rotator_library.providers.openai_codex_auth_base import OpenAICodexAuthBase


def _build_jwt(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}

    def b64url(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{b64url(header)}.{b64url(payload)}.signature"


def test_decode_jwt_helper_valid_token():
    auth = OpenAICodexAuthBase()
    payload = {
        "sub": "user-123",
        "email": "user@example.com",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"},
    }
    token = _build_jwt(payload)

    decoded = auth._decode_jwt_unverified(token)
    assert decoded is not None
    assert decoded["sub"] == "user-123"


def test_decode_jwt_helper_malformed_token():
    auth = OpenAICodexAuthBase()

    assert auth._decode_jwt_unverified("not-a-jwt") is None
    assert auth._decode_jwt_unverified("a.b") is None


def test_decode_jwt_helper_missing_claims_fallbacks():
    auth = OpenAICodexAuthBase()

    payload = {"sub": "fallback-sub", "exp": int(time.time()) + 300}
    token = _build_jwt(payload)

    decoded = auth._decode_jwt_unverified(token)
    email = auth._extract_email_from_payload(decoded)
    account_id = auth._extract_account_id_from_payload(decoded)

    assert email == "fallback-sub"  # email -> sub fallback chain
    assert account_id is None


def test_expiry_logic_with_proactive_buffer_and_true_expiry():
    auth = OpenAICodexAuthBase()

    now_ms = int(time.time() * 1000)

    # still valid (outside proactive buffer)
    fresh = {"expiry_date": now_ms + 20 * 60 * 1000}
    assert auth._is_token_expired(fresh) is False
    assert auth._is_token_truly_expired(fresh) is False

    # proactive refresh window (expired for refresh, still truly valid)
    near_expiry = {"expiry_date": now_ms + 60 * 1000}
    assert auth._is_token_expired(near_expiry) is True
    assert auth._is_token_truly_expired(near_expiry) is False

    # truly expired
    expired = {"expiry_date": now_ms - 60 * 1000}
    assert auth._is_token_expired(expired) is True
    assert auth._is_token_truly_expired(expired) is True


@pytest.mark.asyncio
async def test_env_loading_legacy_and_numbered(monkeypatch):
    auth = OpenAICodexAuthBase()

    payload = {
        "sub": "env-user",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_env"},
    }
    access = _build_jwt(payload)
    refresh = "rt_env"

    monkeypatch.setenv("OPENAI_CODEX_ACCESS_TOKEN", access)
    monkeypatch.setenv("OPENAI_CODEX_REFRESH_TOKEN", refresh)

    # legacy load
    legacy = auth._load_from_env("0")
    assert legacy is not None
    assert legacy["access_token"] == access
    assert legacy["_proxy_metadata"]["loaded_from_env"] is True
    assert legacy["_proxy_metadata"]["account_id"] == "acct_env"

    # numbered load via env:// path
    payload_n = {
        "email": "numbered@example.com",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_num"},
    }
    access_n = _build_jwt(payload_n)
    monkeypatch.setenv("OPENAI_CODEX_1_ACCESS_TOKEN", access_n)
    monkeypatch.setenv("OPENAI_CODEX_1_REFRESH_TOKEN", "rt_num")

    creds = await auth._load_credentials("env://openai_codex/1")
    assert creds["access_token"] == access_n
    assert creds["_proxy_metadata"]["env_credential_index"] == "1"
    assert creds["_proxy_metadata"]["account_id"] == "acct_num"


@pytest.mark.asyncio
async def test_save_load_round_trip_with_proxy_metadata(tmp_path: Path):
    auth = OpenAICodexAuthBase()
    cred_path = tmp_path / "openai_codex_oauth_1.json"

    payload = {
        "email": "roundtrip@example.com",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_roundtrip"},
    }
    access = _build_jwt(payload)

    creds = {
        "access_token": access,
        "refresh_token": "rt_roundtrip",
        "id_token": _build_jwt(payload),
        "expiry_date": int((time.time() + 3600) * 1000),
        "token_uri": "https://auth.openai.com/oauth/token",
        "_proxy_metadata": {
            "email": "roundtrip@example.com",
            "account_id": "acct_roundtrip",
            "last_check_timestamp": time.time(),
            "loaded_from_env": False,
            "env_credential_index": None,
        },
    }

    assert await auth._save_credentials(str(cred_path), creds) is True

    # clear cache to verify disk round-trip
    auth._credentials_cache.clear()
    loaded = await auth._load_credentials(str(cred_path))

    assert loaded["refresh_token"] == "rt_roundtrip"
    assert loaded["_proxy_metadata"]["email"] == "roundtrip@example.com"
    assert loaded["_proxy_metadata"]["account_id"] == "acct_roundtrip"


@pytest.mark.asyncio
async def test_is_credential_available_reauth_queue_and_ttl_cleanup():
    auth = OpenAICodexAuthBase()
    path = "/tmp/openai_codex_oauth_1.json"

    # credential in active re-auth queue => unavailable
    auth._unavailable_credentials[path] = time.time()
    assert auth.is_credential_available(path) is False

    # stale unavailable entry should auto-clean and become available
    auth._unavailable_credentials[path] = time.time() - 999
    auth._queued_credentials.add(path)
    assert auth.is_credential_available(path) is True
    assert path not in auth._unavailable_credentials

    # truly expired credential should be unavailable
    auth._credentials_cache[path] = {
        "expiry_date": int((time.time() - 10) * 1000),
        "_proxy_metadata": {"loaded_from_env": False},
    }
    assert auth.is_credential_available(path) is False

    # let background queue task schedule to avoid un-awaited coroutine warnings
    await asyncio.sleep(0)
