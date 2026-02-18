import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from rotator_library.error_handler import CredentialNeedsReauthError
from rotator_library.providers.openai_codex_auth_base import (
    CALLBACK_PATH,
    LEGACY_CALLBACK_PATH,
    TOKEN_ENDPOINT,
    OpenAICodexAuthBase,
)


def _build_jwt(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}

    def b64url(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{b64url(header)}.{b64url(payload)}.signature"


def test_callback_paths_match_codex_oauth_client_registration():
    assert CALLBACK_PATH == "/auth/callback"
    assert LEGACY_CALLBACK_PATH == "/oauth2callback"


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


def test_ensure_proxy_metadata_prefers_id_token_explicit_email():
    auth = OpenAICodexAuthBase()

    access_payload = {
        "sub": "workspace-sub-shared",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_workspace"},
    }
    id_payload = {
        "email": "real-user@example.com",
        "sub": "user-sub-123",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_workspace"},
    }

    creds = {
        "access_token": _build_jwt(access_payload),
        "id_token": _build_jwt(id_payload),
        "refresh_token": "rt_test",
    }

    auth._ensure_proxy_metadata(creds)

    assert creds["_proxy_metadata"]["email"] == "real-user@example.com"
    assert creds["_proxy_metadata"]["account_id"] == "acct_workspace"


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


def test_find_existing_credential_identity_allows_same_email_different_account(tmp_path: Path):
    auth = OpenAICodexAuthBase()

    existing = tmp_path / "openai_codex_oauth_1.json"
    existing.write_text(
        json.dumps(
            {
                "_proxy_metadata": {
                    "email": "shared@example.com",
                    "account_id": "acct_original",
                }
            }
        )
    )

    # Different account_id with same email should NOT be treated as an update target.
    match = auth._find_existing_credential_by_identity(
        email="shared@example.com",
        account_id="acct_new",
        base_dir=tmp_path,
    )
    assert match is None

    # Exact account_id + email should still match.
    match_same_identity = auth._find_existing_credential_by_identity(
        email="shared@example.com",
        account_id="acct_original",
        base_dir=tmp_path,
    )
    assert match_same_identity == existing

    # Email fallback should work when account_id is unknown.
    match_email_fallback = auth._find_existing_credential_by_identity(
        email="shared@example.com",
        account_id=None,
        base_dir=tmp_path,
    )
    assert match_email_fallback == existing


def test_find_existing_credential_identity_allows_same_account_different_email(tmp_path: Path):
    auth = OpenAICodexAuthBase()

    existing = tmp_path / "openai_codex_oauth_1.json"
    existing.write_text(
        json.dumps(
            {
                "_proxy_metadata": {
                    "email": "first@example.com",
                    "account_id": "acct_workspace",
                }
            }
        )
    )

    # Same account_id but different email should not auto-update when both
    # identifiers are available (prevents workspace-level collisions).
    match = auth._find_existing_credential_by_identity(
        email="second@example.com",
        account_id="acct_workspace",
        base_dir=tmp_path,
    )
    assert match is None


@pytest.mark.asyncio
async def test_setup_credential_creates_new_file_for_same_email_new_account(tmp_path: Path):
    auth = OpenAICodexAuthBase()

    existing = tmp_path / "openai_codex_oauth_1.json"
    existing.write_text(
        json.dumps(
            {
                "access_token": "old_access",
                "refresh_token": "old_refresh",
                "expiry_date": int((time.time() + 3600) * 1000),
                "token_uri": "https://auth.openai.com/oauth/token",
                "_proxy_metadata": {
                    "email": "shared@example.com",
                    "account_id": "acct_original",
                    "loaded_from_env": False,
                    "env_credential_index": None,
                },
            }
        )
    )

    async def fake_initialize_token(_creds):
        return {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "id_token": "new_id",
            "expiry_date": int((time.time() + 3600) * 1000),
            "token_uri": "https://auth.openai.com/oauth/token",
            "_proxy_metadata": {
                "email": "shared@example.com",
                "account_id": "acct_new",
                "loaded_from_env": False,
                "env_credential_index": None,
            },
        }

    auth.initialize_token = fake_initialize_token

    result = await auth.setup_credential(base_dir=tmp_path)

    assert result.success is True
    assert result.is_update is False
    assert result.file_path is not None
    assert result.file_path.endswith("openai_codex_oauth_2.json")

    files = sorted(p.name for p in tmp_path.glob("openai_codex_oauth_*.json"))
    assert files == ["openai_codex_oauth_1.json", "openai_codex_oauth_2.json"]


@pytest.mark.asyncio
async def test_setup_credential_creates_new_file_for_same_account_new_email(tmp_path: Path):
    auth = OpenAICodexAuthBase()

    existing = tmp_path / "openai_codex_oauth_1.json"
    existing.write_text(
        json.dumps(
            {
                "access_token": "old_access",
                "refresh_token": "old_refresh",
                "expiry_date": int((time.time() + 3600) * 1000),
                "token_uri": "https://auth.openai.com/oauth/token",
                "_proxy_metadata": {
                    "email": "first@example.com",
                    "account_id": "acct_workspace",
                    "loaded_from_env": False,
                    "env_credential_index": None,
                },
            }
        )
    )

    async def fake_initialize_token(_creds):
        return {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "id_token": "new_id",
            "expiry_date": int((time.time() + 3600) * 1000),
            "token_uri": "https://auth.openai.com/oauth/token",
            "_proxy_metadata": {
                "email": "second@example.com",
                "account_id": "acct_workspace",
                "loaded_from_env": False,
                "env_credential_index": None,
            },
        }

    auth.initialize_token = fake_initialize_token

    result = await auth.setup_credential(base_dir=tmp_path)

    assert result.success is True
    assert result.is_update is False
    assert result.file_path is not None
    assert result.file_path.endswith("openai_codex_oauth_2.json")

    files = sorted(p.name for p in tmp_path.glob("openai_codex_oauth_*.json"))
    assert files == ["openai_codex_oauth_1.json", "openai_codex_oauth_2.json"]


@pytest.mark.asyncio
async def test_queue_refresh_deduplicates_under_concurrency(monkeypatch):
    auth = OpenAICodexAuthBase()
    path = "/tmp/openai_codex_oauth_1.json"

    async def no_op_queue_processor_start():
        return None

    monkeypatch.setattr(auth, "_ensure_queue_processor_running", no_op_queue_processor_start)

    await asyncio.gather(
        *[
            auth._queue_refresh(path, force=False, needs_reauth=False)
            for _ in range(25)
        ]
    )

    assert auth._refresh_queue.qsize() == 1

    queued_path, queued_force = await auth._refresh_queue.get()
    assert queued_path == path
    assert queued_force is False
    auth._refresh_queue.task_done()


@pytest.mark.asyncio
async def test_refresh_invalid_grant_queues_reauth_sync(tmp_path: Path, monkeypatch):
    auth = OpenAICodexAuthBase()
    cred_path = tmp_path / "openai_codex_oauth_1.json"

    payload = {
        "sub": "refresh-user",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_refresh"},
    }

    cred_path.write_text(
        json.dumps(
            {
                "access_token": _build_jwt(payload),
                "refresh_token": "rt_refresh",
                "id_token": _build_jwt(payload),
                "expiry_date": int((time.time() - 60) * 1000),
                "token_uri": "https://auth.openai.com/oauth/token",
                "_proxy_metadata": {
                    "email": "refresh@example.com",
                    "account_id": "acct_refresh",
                    "loaded_from_env": False,
                    "env_credential_index": None,
                },
            }
        )
    )

    queued: list[tuple[str, bool, bool]] = []

    async def capture_queue_refresh(path_arg: str, force: bool = False, needs_reauth: bool = False):
        queued.append((path_arg, force, needs_reauth))

    monkeypatch.setattr(auth, "_queue_refresh", capture_queue_refresh)

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                status_code=400,
                json={
                    "error": "invalid_grant",
                    "error_description": "refresh token revoked",
                },
            )
        )

        with pytest.raises(CredentialNeedsReauthError):
            await auth._refresh_token(str(cred_path), force=True)

    assert queued == [(str(cred_path), True, True)]
