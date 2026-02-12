import json
import os
import time
from pathlib import Path

from rotator_library.credential_manager import CredentialManager


def _build_jwt(payload: dict) -> str:
    import base64

    header = {"alg": "HS256", "typ": "JWT"}

    def b64url(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{b64url(header)}.{b64url(payload)}.sig"


def _write_codex_auth_json(path: Path):
    payload = {
        "email": "single@example.com",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_single"},
    }
    data = {
        "auth_mode": "oauth",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _build_jwt(payload),
            "access_token": _build_jwt(payload),
            "refresh_token": "rt_single",
            "account_id": "acct_single",
        },
        "last_refresh": "2026-02-12T00:00:00Z",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _write_codex_accounts_json(path: Path):
    payload_a = {
        "email": "multi-a@example.com",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_a"},
    }
    payload_b = {
        "email": "multi-b@example.com",
        "exp": int(time.time()) + 7200,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_b"},
    }

    data = {
        "schemaVersion": 1,
        "activeLabel": "A",
        "accounts": [
            {
                "label": "A",
                "accountId": "acct_a",
                "access": _build_jwt(payload_a),
                "refresh": "rt_a",
                "idToken": _build_jwt(payload_a),
                "expires": int((time.time() + 3600) * 1000),
            },
            {
                "label": "B",
                "accountId": "acct_b",
                "access": _build_jwt(payload_b),
                "refresh": "rt_b",
                "idToken": _build_jwt(payload_b),
                "expires": int((time.time() + 7200) * 1000),
            },
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def test_import_from_codex_auth_and_accounts_formats(tmp_path: Path):
    oauth_dir = tmp_path / "oauth_creds"
    manager = CredentialManager(env_vars={}, oauth_dir=oauth_dir)

    auth_json = tmp_path / ".codex" / "auth.json"
    accounts_json = tmp_path / ".codex-accounts.json"
    _write_codex_auth_json(auth_json)
    _write_codex_accounts_json(accounts_json)

    imported = manager._import_openai_codex_cli_credentials(
        auth_json_path=auth_json,
        accounts_json_path=accounts_json,
    )

    # one from auth.json + two from accounts.json
    assert len(imported) == 3

    imported_files = sorted(oauth_dir.glob("openai_codex_oauth_*.json"))
    assert len(imported_files) == 3

    payload = json.loads(imported_files[0].read_text())
    assert payload["refresh_token"].startswith("rt_")
    assert "_proxy_metadata" in payload
    assert payload["_proxy_metadata"].get("account_id")


def test_explicit_openai_codex_oauth_path_auth_json_is_normalized(tmp_path: Path):
    oauth_dir = tmp_path / "oauth_creds"

    auth_json = tmp_path / ".codex" / "auth.json"
    _write_codex_auth_json(auth_json)

    manager = CredentialManager(
        env_vars={"OPENAI_CODEX_OAUTH_1": str(auth_json)},
        oauth_dir=oauth_dir,
    )
    discovered = manager.discover_and_prepare()

    assert "openai_codex" in discovered
    assert len(discovered["openai_codex"]) == 1

    imported_file = oauth_dir / "openai_codex_oauth_1.json"
    payload = json.loads(imported_file.read_text())

    # normalized proxy schema at root level (not nested under "tokens")
    assert "tokens" not in payload
    assert isinstance(payload.get("access_token"), str)
    assert isinstance(payload.get("refresh_token"), str)
    assert payload.get("token_uri") == "https://auth.openai.com/oauth/token"
    assert "_proxy_metadata" in payload


def test_skip_import_when_env_openai_codex_credentials_exist(tmp_path: Path):
    oauth_dir = tmp_path / "oauth_creds"
    manager = CredentialManager(
        env_vars={
            "OPENAI_CODEX_ACCESS_TOKEN": "env_access",
            "OPENAI_CODEX_REFRESH_TOKEN": "env_refresh",
        },
        oauth_dir=oauth_dir,
    )

    discovered = manager.discover_and_prepare()

    assert discovered["openai_codex"] == ["env://openai_codex/0"]
    assert list(oauth_dir.glob("openai_codex_oauth_*.json")) == []


def test_skip_import_when_local_openai_codex_credentials_exist(tmp_path: Path):
    oauth_dir = tmp_path / "oauth_creds"
    oauth_dir.mkdir(parents=True, exist_ok=True)

    existing = oauth_dir / "openai_codex_oauth_1.json"
    existing.write_text(
        json.dumps(
            {
                "access_token": "existing",
                "refresh_token": "existing_rt",
                "expiry_date": int((time.time() + 3600) * 1000),
                "token_uri": "https://auth.openai.com/oauth/token",
                "_proxy_metadata": {
                    "email": "existing@example.com",
                    "account_id": "acct_existing",
                    "last_check_timestamp": time.time(),
                    "loaded_from_env": False,
                    "env_credential_index": None,
                },
            },
            indent=2,
        )
    )

    manager = CredentialManager(env_vars={}, oauth_dir=oauth_dir)
    discovered = manager.discover_and_prepare()

    assert "openai_codex" in discovered
    assert discovered["openai_codex"] == [str(existing.resolve())]


def test_malformed_codex_source_files_are_handled_gracefully(tmp_path: Path):
    oauth_dir = tmp_path / "oauth_creds"
    manager = CredentialManager(env_vars={}, oauth_dir=oauth_dir)

    auth_json = tmp_path / ".codex" / "auth.json"
    accounts_json = tmp_path / ".codex-accounts.json"
    auth_json.parent.mkdir(parents=True, exist_ok=True)

    auth_json.write_text("{not valid json")
    accounts_json.write_text(json.dumps({"schemaVersion": 1, "accounts": ["bad-entry"]}))

    imported = manager._import_openai_codex_cli_credentials(
        auth_json_path=auth_json,
        accounts_json_path=accounts_json,
    )

    assert imported == []
    assert list(oauth_dir.glob("openai_codex_oauth_*.json")) == []


def test_codex_source_files_never_modified_during_import(tmp_path: Path):
    oauth_dir = tmp_path / "oauth_creds"
    manager = CredentialManager(env_vars={}, oauth_dir=oauth_dir)

    auth_json = tmp_path / ".codex" / "auth.json"
    accounts_json = tmp_path / ".codex-accounts.json"
    _write_codex_auth_json(auth_json)
    _write_codex_accounts_json(accounts_json)

    auth_before = auth_json.read_text()
    accounts_before = accounts_json.read_text()

    manager._import_openai_codex_cli_credentials(
        auth_json_path=auth_json,
        accounts_json_path=accounts_json,
    )

    assert auth_json.read_text() == auth_before
    assert accounts_json.read_text() == accounts_before
