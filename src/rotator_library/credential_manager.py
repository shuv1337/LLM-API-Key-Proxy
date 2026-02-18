# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 Mirrowel

import os
import re
import json
import time
import shutil
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Union, Any, Tuple

from .utils.openai_codex_jwt import (
    decode_jwt_unverified,
    extract_account_id_from_payload,
    extract_email_from_payload,
    extract_expiry_ms_from_payload,
)
from .utils.paths import get_oauth_dir

lib_logger = logging.getLogger("rotator_library")

# Standard directories where tools like `gemini login` store credentials.
DEFAULT_OAUTH_DIRS = {
    "gemini_cli": Path.home() / ".gemini",
    "qwen_code": Path.home() / ".qwen",
    "iflow": Path.home() / ".iflow",
    "antigravity": Path.home() / ".antigravity",
    "openai_codex": Path.home() / ".codex",  # import source context only
    # Add other providers like 'claude' here if they have a standard CLI path
}

# OAuth providers that support environment variable-based credentials
# Maps provider name to the ENV_PREFIX used by the provider
ENV_OAUTH_PROVIDERS = {
    "gemini_cli": "GEMINI_CLI",
    "antigravity": "ANTIGRAVITY",
    "qwen_code": "QWEN_CODE",
    "iflow": "IFLOW",
    "openai_codex": "OPENAI_CODEX",
}


class CredentialManager:
    """
    Discovers OAuth credential files from standard locations, copies them locally,
    and updates the configuration to use the local paths.

    Also discovers environment variable-based OAuth credentials for stateless deployments.
    Supports two env var formats:

    1. Single credential (legacy): PROVIDER_ACCESS_TOKEN, PROVIDER_REFRESH_TOKEN
    2. Multiple credentials (numbered): PROVIDER_1_ACCESS_TOKEN, PROVIDER_2_ACCESS_TOKEN, etc.

    When env-based credentials are detected, virtual paths like "env://provider/1" are created.
    """

    def __init__(
        self,
        env_vars: Dict[str, str],
        oauth_dir: Optional[Union[Path, str]] = None,
    ):
        """
        Initialize the CredentialManager.

        Args:
            env_vars: Dictionary of environment variables (typically os.environ).
            oauth_dir: Directory for storing OAuth credentials.
                       If None, uses get_oauth_dir() which respects EXE vs script mode.
        """
        self.env_vars = env_vars
        self.oauth_base_dir = Path(oauth_dir) if oauth_dir else get_oauth_dir()
        self.oauth_base_dir.mkdir(parents=True, exist_ok=True)

    def _discover_env_oauth_credentials(self) -> Dict[str, List[str]]:
        """
        Discover OAuth credentials defined via environment variables.

        Supports two formats:
        1. Single credential: ANTIGRAVITY_ACCESS_TOKEN + ANTIGRAVITY_REFRESH_TOKEN
        2. Multiple credentials: ANTIGRAVITY_1_ACCESS_TOKEN + ANTIGRAVITY_1_REFRESH_TOKEN, etc.

        Returns:
            Dict mapping provider name to list of virtual paths (e.g., "env://antigravity/1")
        """
        env_credentials: Dict[str, Set[str]] = {}

        for provider, env_prefix in ENV_OAUTH_PROVIDERS.items():
            found_indices: Set[str] = set()

            # Check for numbered credentials (PROVIDER_N_ACCESS_TOKEN pattern)
            # Pattern: ANTIGRAVITY_1_ACCESS_TOKEN, ANTIGRAVITY_2_ACCESS_TOKEN, etc.
            numbered_pattern = re.compile(rf"^{env_prefix}_(\d+)_ACCESS_TOKEN$")

            for key in self.env_vars.keys():
                match = numbered_pattern.match(key)
                if match:
                    index = match.group(1)
                    # Verify refresh token also exists
                    refresh_key = f"{env_prefix}_{index}_REFRESH_TOKEN"
                    if refresh_key in self.env_vars and self.env_vars[refresh_key]:
                        found_indices.add(index)

            # Check for legacy single credential (PROVIDER_ACCESS_TOKEN pattern)
            # Only use this if no numbered credentials exist
            if not found_indices:
                access_key = f"{env_prefix}_ACCESS_TOKEN"
                refresh_key = f"{env_prefix}_REFRESH_TOKEN"
                if (
                    access_key in self.env_vars
                    and self.env_vars[access_key]
                    and refresh_key in self.env_vars
                    and self.env_vars[refresh_key]
                ):
                    # Use "0" as the index for legacy single credential
                    found_indices.add("0")

            if found_indices:
                env_credentials[provider] = found_indices
                lib_logger.info(
                    f"Found {len(found_indices)} env-based credential(s) for {provider}"
                )

        # Convert to virtual paths
        result: Dict[str, List[str]] = {}
        for provider, indices in env_credentials.items():
            # Sort indices numerically for consistent ordering
            sorted_indices = sorted(indices, key=lambda x: int(x))
            result[provider] = [f"env://{provider}/{idx}" for idx in sorted_indices]

        return result

    # -------------------------------------------------------------------------
    # OpenAI Codex first-run import helpers
    # -------------------------------------------------------------------------

    def _extract_codex_identity(
        self,
        access_token: str,
        id_token: Optional[str],
    ) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        """
        Extract (account_id, email, exp_ms) from Codex JWTs.

        Priority:
        - account_id: access_token -> id_token
        - email: id_token -> access_token
        - exp: access_token -> id_token
        """
        access_payload = decode_jwt_unverified(access_token)
        id_payload = decode_jwt_unverified(id_token) if id_token else None

        account_id = extract_account_id_from_payload(access_payload) or extract_account_id_from_payload(
            id_payload
        )
        email = extract_email_from_payload(id_payload) or extract_email_from_payload(access_payload)
        exp_ms = extract_expiry_ms_from_payload(access_payload) or extract_expiry_ms_from_payload(
            id_payload
        )

        return account_id, email, exp_ms

    def _normalize_openai_codex_auth_json_record(self, auth_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize ~/.codex/auth.json format to proxy schema."""
        tokens = auth_data.get("tokens")
        if not isinstance(tokens, dict):
            return None

        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        id_token = tokens.get("id_token")

        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            return None

        account_id, email, exp_ms = self._extract_codex_identity(access_token, id_token)

        # Respect explicit account_id from source tokens if present
        explicit_account = tokens.get("account_id")
        if isinstance(explicit_account, str) and explicit_account.strip():
            account_id = explicit_account.strip()

        if exp_ms is None:
            # conservative fallback to 5 minutes from now
            exp_ms = int((time.time() + 300) * 1000)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "expiry_date": exp_ms,
            "token_uri": "https://auth.openai.com/oauth/token",
            "_proxy_metadata": {
                "email": email,
                "account_id": account_id,
                "last_check_timestamp": time.time(),
                "loaded_from_env": False,
                "env_credential_index": None,
            },
        }

    def _normalize_openai_codex_accounts_record(self, account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one ~/.codex-accounts.json account entry to proxy schema."""
        access_token = account.get("access")
        refresh_token = account.get("refresh")
        id_token = account.get("idToken")

        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            return None

        account_id, email, exp_ms = self._extract_codex_identity(access_token, id_token)

        explicit_account = account.get("accountId")
        if isinstance(explicit_account, str) and explicit_account.strip():
            account_id = explicit_account.strip()

        label = account.get("label")
        if not email and isinstance(label, str) and label.strip():
            email = label.strip()

        expires = account.get("expires")
        if isinstance(expires, (int, float)):
            exp_ms = int(expires)

        if exp_ms is None:
            exp_ms = int((time.time() + 300) * 1000)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "expiry_date": exp_ms,
            "token_uri": "https://auth.openai.com/oauth/token",
            "_proxy_metadata": {
                "email": email,
                "account_id": account_id,
                "last_check_timestamp": time.time(),
                "loaded_from_env": False,
                "env_credential_index": None,
            },
        }

    def _dedupe_openai_codex_records(
        self,
        records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Deduplicate normalized Codex credential records by account/email identity."""
        unique: List[Dict[str, Any]] = []
        seen_account_ids: Set[str] = set()
        seen_emails: Set[str] = set()

        for record in records:
            metadata = record.get("_proxy_metadata", {})
            account_id = metadata.get("account_id")
            email = metadata.get("email")

            if isinstance(account_id, str) and account_id:
                if account_id in seen_account_ids:
                    continue
                seen_account_ids.add(account_id)

            if isinstance(email, str) and email:
                if email in seen_emails:
                    continue
                seen_emails.add(email)

            unique.append(record)

        return unique

    def _import_openai_codex_cli_credentials(
        self,
        auth_json_path: Optional[Path] = None,
        accounts_json_path: Optional[Path] = None,
    ) -> List[str]:
        """
        First-run import from Codex CLI stores into local oauth_creds/.

        Source files are read-only:
        - ~/.codex/auth.json (single account)
        - ~/.codex-accounts.json (multi-account)
        """
        auth_json_path = auth_json_path or (Path.home() / ".codex" / "auth.json")
        accounts_json_path = accounts_json_path or (Path.home() / ".codex-accounts.json")

        normalized_records: List[Dict[str, Any]] = []

        # Source 1: ~/.codex/auth.json
        if auth_json_path.exists():
            try:
                with open(auth_json_path, "r") as f:
                    auth_data = json.load(f)

                if isinstance(auth_data, dict):
                    record = self._normalize_openai_codex_auth_json_record(auth_data)
                    if record:
                        normalized_records.append(record)
                    else:
                        lib_logger.warning(
                            "OpenAI Codex import: skipping malformed ~/.codex/auth.json record"
                        )
                else:
                    lib_logger.warning(
                        "OpenAI Codex import: ~/.codex/auth.json root is not an object"
                    )
            except Exception as e:
                lib_logger.warning(
                    f"OpenAI Codex import: failed to parse ~/.codex/auth.json: {e}"
                )

        # Source 2: ~/.codex-accounts.json
        if accounts_json_path.exists():
            try:
                with open(accounts_json_path, "r") as f:
                    accounts_data = json.load(f)

                accounts = []
                if isinstance(accounts_data, dict):
                    raw_accounts = accounts_data.get("accounts")
                    if isinstance(raw_accounts, list):
                        accounts = raw_accounts
                elif isinstance(accounts_data, list):
                    accounts = accounts_data

                if not accounts:
                    lib_logger.warning(
                        "OpenAI Codex import: ~/.codex-accounts.json has no accounts list"
                    )

                for idx, account in enumerate(accounts):
                    if not isinstance(account, dict):
                        lib_logger.warning(
                            f"OpenAI Codex import: skipping malformed account entry #{idx + 1}"
                        )
                        continue

                    record = self._normalize_openai_codex_accounts_record(account)
                    if record:
                        normalized_records.append(record)
                    else:
                        lib_logger.warning(
                            f"OpenAI Codex import: skipping malformed account entry #{idx + 1}"
                        )

            except Exception as e:
                lib_logger.warning(
                    f"OpenAI Codex import: failed to parse ~/.codex-accounts.json: {e}"
                )

        if not normalized_records:
            return []

        deduped_records = self._dedupe_openai_codex_records(normalized_records)

        imported_paths: List[str] = []
        for i, record in enumerate(deduped_records, 1):
            local_path = self.oauth_base_dir / f"openai_codex_oauth_{i}.json"
            try:
                with open(local_path, "w") as f:
                    json.dump(record, f, indent=2)
                imported_paths.append(str(local_path.resolve()))
            except Exception as e:
                lib_logger.error(
                    f"OpenAI Codex import: failed writing '{local_path.name}': {e}"
                )

        if imported_paths:
            identifiers = []
            for p in imported_paths:
                try:
                    with open(p, "r") as f:
                        payload = json.load(f)
                    meta = payload.get("_proxy_metadata", {})
                    identifiers.append(
                        meta.get("email") or meta.get("account_id") or Path(p).name
                    )
                except Exception:
                    identifiers.append(Path(p).name)

            lib_logger.info(
                "OpenAI Codex first-run import complete: "
                f"{len(imported_paths)} credential(s) imported ({', '.join(str(x) for x in identifiers)})"
            )

        return imported_paths

    def _import_openai_codex_explicit_paths(self, source_paths: List[Path]) -> List[str]:
        """
        Import OpenAI Codex credentials from explicit OPENAI_CODEX_OAUTH_* paths.

        Supports:
        - Raw Codex CLI files (`~/.codex/auth.json`, `~/.codex-accounts.json`)
        - Already-normalized proxy credential JSON files

        Returns local normalized/copied paths under oauth_creds/.
        """
        if not source_paths:
            return []

        normalized_records: List[Dict[str, Any]] = []
        passthrough_paths: List[Path] = []

        for source_path in sorted(source_paths):
            try:
                with open(source_path, "r") as f:
                    payload = json.load(f)
            except Exception as e:
                lib_logger.warning(
                    f"OpenAI Codex explicit import: failed to parse '{source_path}': {e}. Falling back to direct copy."
                )
                passthrough_paths.append(source_path)
                continue

            # Raw ~/.codex/auth.json shape
            if isinstance(payload, dict) and isinstance(payload.get("tokens"), dict):
                record = self._normalize_openai_codex_auth_json_record(payload)
                if record:
                    normalized_records.append(record)
                    continue

            # Raw ~/.codex-accounts.json shape (object or root list)
            accounts: List[Any] = []
            if isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
                accounts = payload.get("accounts")
            elif isinstance(payload, list):
                accounts = payload

            if accounts:
                converted = 0
                for idx, account in enumerate(accounts):
                    if not isinstance(account, dict):
                        lib_logger.warning(
                            f"OpenAI Codex explicit import: skipping malformed account entry #{idx + 1} from '{source_path.name}'"
                        )
                        continue

                    record = self._normalize_openai_codex_accounts_record(account)
                    if record:
                        normalized_records.append(record)
                        converted += 1

                if converted > 0:
                    continue

            # Already-normalized proxy format
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("access_token"), str)
                and isinstance(payload.get("refresh_token"), str)
            ):
                passthrough_paths.append(source_path)
                continue

            # Unknown shape: preserve existing behavior (copy as-is)
            passthrough_paths.append(source_path)

        deduped_records = self._dedupe_openai_codex_records(normalized_records)

        imported_paths: List[str] = []
        next_index = 1

        # Write normalized records first
        for record in deduped_records:
            local_path = self.oauth_base_dir / f"openai_codex_oauth_{next_index}.json"
            try:
                with open(local_path, "w") as f:
                    json.dump(record, f, indent=2)
                imported_paths.append(str(local_path.resolve()))
                next_index += 1
            except Exception as e:
                lib_logger.error(
                    f"OpenAI Codex explicit import: failed writing '{local_path.name}': {e}"
                )

        # Copy passthrough files after normalized ones
        for source_path in passthrough_paths:
            local_path = self.oauth_base_dir / f"openai_codex_oauth_{next_index}.json"
            try:
                shutil.copy(source_path, local_path)
                imported_paths.append(str(local_path.resolve()))
                next_index += 1
            except Exception as e:
                lib_logger.error(
                    f"OpenAI Codex explicit import: failed to copy '{source_path}' -> '{local_path}': {e}"
                )

        if imported_paths:
            lib_logger.info(
                "OpenAI Codex explicit-path import complete: "
                f"{len(imported_paths)} credential(s) prepared"
            )

        return imported_paths

    def discover_and_prepare(self) -> Dict[str, List[str]]:
        lib_logger.info("Starting automated OAuth credential discovery...")
        final_config = {}

        # PHASE 1: Discover environment variable-based OAuth credentials
        # These take priority for stateless deployments
        env_oauth_creds = self._discover_env_oauth_credentials()
        for provider, virtual_paths in env_oauth_creds.items():
            lib_logger.info(
                f"Using {len(virtual_paths)} env-based credential(s) for {provider}"
            )
            final_config[provider] = virtual_paths

        # Extract OAuth file paths from environment variables
        env_oauth_paths = {}
        for key, value in self.env_vars.items():
            if "_OAUTH_" in key:
                provider = key.split("_OAUTH_")[0].lower()
                if provider not in env_oauth_paths:
                    env_oauth_paths[provider] = []
                if value:  # Only consider non-empty values
                    env_oauth_paths[provider].append(value)

        # PHASE 2: Discover file-based OAuth credentials
        for provider, default_dir in DEFAULT_OAUTH_DIRS.items():
            # Skip if already discovered from environment variables
            if provider in final_config:
                lib_logger.debug(
                    f"Skipping file discovery for {provider} - using env-based credentials"
                )
                continue

            # Check for existing local credentials first. If found, use them and skip discovery.
            local_provider_creds = sorted(
                list(self.oauth_base_dir.glob(f"{provider}_oauth_*.json"))
            )
            if local_provider_creds:
                lib_logger.info(
                    f"Found {len(local_provider_creds)} existing local credential(s) for {provider}. Skipping discovery."
                )
                final_config[provider] = [
                    str(p.resolve()) for p in local_provider_creds
                ]
                continue

            # If no local credentials exist, proceed with one-time import/copy.
            discovered_paths = set()

            # 1. Add paths from environment variables first, as they are overrides
            for path_str in env_oauth_paths.get(provider, []):
                path = Path(path_str).expanduser()
                if path.exists():
                    discovered_paths.add(path)

            # 2. Provider-specific first-run import for OpenAI Codex
            # Trigger only when:
            # - provider == openai_codex
            # - no local openai_codex_oauth_*.json already exist (checked above)
            # - no env-based OPENAI_CODEX credentials were selected (provider not in final_config)
            # - no explicit OPENAI_CODEX_OAUTH_* file paths were provided
            if provider == "openai_codex" and not discovered_paths:
                imported = self._import_openai_codex_cli_credentials()
                if imported:
                    final_config[provider] = imported
                    continue

            # 3. Provider-specific explicit-path import handling for OpenAI Codex
            # This normalizes raw ~/.codex/auth.json / ~/.codex-accounts.json when
            # supplied via OPENAI_CODEX_OAUTH_* env vars.
            if provider == "openai_codex" and discovered_paths:
                imported = self._import_openai_codex_explicit_paths(
                    sorted(list(discovered_paths))
                )
                if imported:
                    final_config[provider] = imported
                    continue

            # 4. Default directory scan remains disabled (local-first policy)
            # if not discovered_paths and default_dir.exists():
            #     for json_file in default_dir.glob('*.json'):
            #         discovered_paths.add(json_file)

            if not discovered_paths:
                lib_logger.debug(f"No credential files found for provider: {provider}")
                continue

            prepared_paths = []
            # Sort paths to ensure consistent numbering for the initial copy
            for i, source_path in enumerate(sorted(list(discovered_paths))):
                account_id = i + 1
                local_filename = f"{provider}_oauth_{account_id}.json"
                local_path = self.oauth_base_dir / local_filename

                try:
                    # Since we've established no local files exist, we can copy directly.
                    shutil.copy(source_path, local_path)
                    lib_logger.info(
                        f"Copied '{source_path.name}' to local pool at '{local_path}'."
                    )
                    prepared_paths.append(str(local_path.resolve()))
                except Exception as e:
                    lib_logger.error(
                        f"Failed to process OAuth file from '{source_path}': {e}"
                    )

            if prepared_paths:
                lib_logger.info(
                    f"Discovered and prepared {len(prepared_paths)} credential(s) for provider: {provider}"
                )
                final_config[provider] = prepared_paths

        lib_logger.info("OAuth credential discovery complete.")
        return final_config
