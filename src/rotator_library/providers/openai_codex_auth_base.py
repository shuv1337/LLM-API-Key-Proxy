# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 Mirrowel

# src/rotator_library/providers/openai_codex_auth_base.py

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlencode

import httpx
from aiohttp import web
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.text import Text

from ..error_handler import CredentialNeedsReauthError
from ..utils.headless_detection import is_headless_environment
from ..utils.reauth_coordinator import get_reauth_coordinator
from ..utils.resilient_io import safe_write_json

lib_logger = logging.getLogger("rotator_library")

# OAuth constants
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
SCOPE = "openid profile email offline_access"
AUTHORIZATION_ENDPOINT = "https://auth.openai.com/oauth/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
# OpenAI Codex OAuth redirect path registered for this client.
# Keep legacy `/oauth2callback` handler for backward compatibility with old URLs.
CALLBACK_PATH = "/auth/callback"
LEGACY_CALLBACK_PATH = "/oauth2callback"
CALLBACK_PORT = 1455
CALLBACK_ENV_VAR = "OPENAI_CODEX_OAUTH_PORT"

# API constants
DEFAULT_API_BASE = "https://chatgpt.com/backend-api"
RESPONSES_ENDPOINT_PATH = "/codex/responses"

# JWT claims
AUTH_CLAIM = "https://api.openai.com/auth"
ACCOUNT_ID_CLAIM = "https://api.openai.com/auth.chatgpt_account_id"

# Refresh when token is close to expiry
REFRESH_EXPIRY_BUFFER_SECONDS = 5 * 60  # 5 minutes

console = Console()


@dataclass
class OpenAICodexCredentialSetupResult:
    """Standardized result structure for OpenAI Codex credential setup operations."""

    success: bool
    file_path: Optional[str] = None
    email: Optional[str] = None
    is_update: bool = False
    error: Optional[str] = None
    credentials: Optional[Dict[str, Any]] = field(default=None, repr=False)


class OAuthCallbackServer:
    """Minimal HTTP server for handling OpenAI Codex OAuth callbacks."""

    SUCCESS_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Authentication successful</title>
</head>
<body>
  <p>Authentication successful. Return to your terminal to continue.</p>
</body>
</html>"""

    def __init__(self, port: int = CALLBACK_PORT):
        self.port = port
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.result_future: Optional[asyncio.Future] = None
        self.expected_state: Optional[str] = None

    async def start(self, expected_state: str):
        """Start callback server on localhost:<port>."""
        self.expected_state = expected_state
        self.result_future = asyncio.Future()

        for callback_path in {CALLBACK_PATH, LEGACY_CALLBACK_PATH}:
            self.app.router.add_get(callback_path, self._handle_callback)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", self.port)
        await self.site.start()

        lib_logger.debug(
            "OpenAI Codex OAuth callback server started on "
            f"localhost:{self.port}{CALLBACK_PATH} "
            f"(legacy alias: {LEGACY_CALLBACK_PATH})"
        )

    async def stop(self):
        """Stop callback server."""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        lib_logger.debug("OpenAI Codex OAuth callback server stopped")

    async def _handle_callback(self, request: web.Request) -> web.Response:
        query = request.query

        if "error" in query:
            error = query.get("error", "unknown_error")
            error_desc = query.get("error_description", "")
            if not self.result_future.done():
                self.result_future.set_exception(
                    ValueError(f"OAuth error: {error} ({error_desc})")
                )
            return web.Response(status=400, text=f"OAuth error: {error}")

        code = query.get("code")
        state = query.get("state", "")

        if not code:
            if not self.result_future.done():
                self.result_future.set_exception(
                    ValueError("Missing authorization code")
                )
            return web.Response(status=400, text="Missing authorization code")

        if state != self.expected_state:
            if not self.result_future.done():
                self.result_future.set_exception(ValueError("State parameter mismatch"))
            return web.Response(status=400, text="State mismatch")

        if not self.result_future.done():
            self.result_future.set_result(code)

        return web.Response(
            status=200,
            text=self.SUCCESS_HTML,
            content_type="text/html",
        )

    async def wait_for_callback(self, timeout: float = 300.0) -> str:
        """Wait for OAuth callback and return auth code."""
        try:
            code = await asyncio.wait_for(self.result_future, timeout=timeout)
            return code
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for OAuth callback")


def get_callback_port() -> int:
    """Get OAuth callback port from env or fallback default."""
    env_value = os.getenv(CALLBACK_ENV_VAR)
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            lib_logger.warning(
                f"Invalid {CALLBACK_ENV_VAR} value: {env_value}, using default {CALLBACK_PORT}"
            )
    return CALLBACK_PORT


class OpenAICodexAuthBase:
    """
    OpenAI Codex OAuth authentication base class.

    Supports:
    - Interactive OAuth Authorization Code + PKCE
    - Token refresh with retry/backoff
    - File + env credential loading (`env://openai_codex/N`)
    - Queue-based refresh and re-auth workflows
    - Credential management APIs for credential_tool
    """

    CALLBACK_PORT = CALLBACK_PORT
    CALLBACK_ENV_VAR = CALLBACK_ENV_VAR

    def __init__(self):
        self._credentials_cache: Dict[str, Dict[str, Any]] = {}
        self._refresh_locks: Dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()

        # Backoff tracking
        self._refresh_failures: Dict[str, int] = {}
        self._next_refresh_after: Dict[str, float] = {}

        # Queue system (normal refresh + interactive re-auth)
        self._refresh_queue: asyncio.Queue = asyncio.Queue()
        self._queue_processor_task: Optional[asyncio.Task] = None

        self._reauth_queue: asyncio.Queue = asyncio.Queue()
        self._reauth_processor_task: Optional[asyncio.Task] = None

        self._queued_credentials: set = set()
        self._unavailable_credentials: Dict[str, float] = {}
        self._unavailable_ttl_seconds: int = 360
        self._queue_tracking_lock = asyncio.Lock()

        self._queue_retry_count: Dict[str, int] = {}

        # Queue configuration
        self._refresh_timeout_seconds: int = 20
        self._refresh_interval_seconds: int = 20
        self._refresh_max_retries: int = 3
        self._reauth_timeout_seconds: int = 300

    # =========================================================================
    # JWT + metadata helpers
    # =========================================================================

    @staticmethod
    def _decode_jwt_unverified(token: str) -> Optional[Dict[str, Any]]:
        """Decode JWT payload without signature verification."""
        if not token or not isinstance(token, str):
            return None

        parts = token.split(".")
        if len(parts) < 2:
            return None

        payload_segment = parts[1]
        padding = "=" * (-len(payload_segment) % 4)

        try:
            payload_bytes = base64.urlsafe_b64decode(payload_segment + padding)
            payload = json.loads(payload_bytes.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    @staticmethod
    def _extract_account_id_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
        """Extract account ID from JWT claims."""
        if not payload:
            return None

        # 1) Direct dotted claim format (requested by plan)
        direct = payload.get(ACCOUNT_ID_CLAIM)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        # 2) Nested object claim format observed in real tokens
        auth_claim = payload.get(AUTH_CLAIM)
        if isinstance(auth_claim, dict):
            nested = auth_claim.get("chatgpt_account_id")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()

        # 3) Fallback organizations[0].id if present
        orgs = payload.get("organizations")
        if isinstance(orgs, list) and orgs:
            first = orgs[0]
            if isinstance(first, dict):
                org_id = first.get("id")
                if isinstance(org_id, str) and org_id.strip():
                    return org_id.strip()

        return None

    @staticmethod
    def _extract_email_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
        """Extract email from JWT payload using fallback chain: email -> sub."""
        if not payload:
            return None

        email = payload.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()

        sub = payload.get("sub")
        if isinstance(sub, str) and sub.strip():
            return sub.strip()

        return None

    @staticmethod
    def _extract_expiry_ms_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[int]:
        """Extract JWT exp claim and convert to milliseconds."""
        if not payload:
            return None

        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return int(float(exp) * 1000)

        return None

    def _populate_metadata_from_tokens(self, creds: Dict[str, Any]) -> None:
        """Populate _proxy_metadata (email/account_id) from access_token or id_token."""
        metadata = creds.setdefault("_proxy_metadata", {})

        access_payload = self._decode_jwt_unverified(creds.get("access_token", ""))
        id_payload = self._decode_jwt_unverified(creds.get("id_token", ""))

        account_id = self._extract_account_id_from_payload(
            access_payload
        ) or self._extract_account_id_from_payload(id_payload)
        email = self._extract_email_from_payload(access_payload) or self._extract_email_from_payload(
            id_payload
        )

        if account_id:
            metadata["account_id"] = account_id

        if email:
            metadata["email"] = email

        # Keep top-level expiry_date synchronized from token exp as fallback
        if not creds.get("expiry_date"):
            expiry_ms = self._extract_expiry_ms_from_payload(access_payload) or self._extract_expiry_ms_from_payload(
                id_payload
            )
            if expiry_ms:
                creds["expiry_date"] = expiry_ms

        metadata["last_check_timestamp"] = time.time()

    def _ensure_proxy_metadata(self, creds: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure credentials include normalized _proxy_metadata fields."""
        metadata = creds.setdefault("_proxy_metadata", {})
        metadata.setdefault("loaded_from_env", False)
        metadata.setdefault("env_credential_index", None)

        self._populate_metadata_from_tokens(creds)

        # Keep top-level token_uri stable for schema consistency
        creds.setdefault("token_uri", TOKEN_ENDPOINT)

        return creds

    # =========================================================================
    # Env + file credential loading
    # =========================================================================

    def _parse_env_credential_path(self, path: str) -> Optional[str]:
        """
        Parse a virtual env:// path and return the credential index.

        Supported formats:
        - env://openai_codex/0  (legacy single)
        - env://openai_codex/1  (numbered)
        """
        if not path.startswith("env://"):
            return None

        raw = path[6:]
        parts = raw.split("/")
        if not parts:
            return None

        provider = parts[0].strip().lower()
        if provider != "openai_codex":
            return None

        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip()

        return "0"

    def _load_from_env(
        self, credential_index: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Load OpenAI Codex OAuth credentials from environment variables.

        Legacy single credential:
        - OPENAI_CODEX_ACCESS_TOKEN
        - OPENAI_CODEX_REFRESH_TOKEN
        - OPENAI_CODEX_EXPIRY_DATE (optional)
        - OPENAI_CODEX_ID_TOKEN (optional)
        - OPENAI_CODEX_ACCOUNT_ID (optional)
        - OPENAI_CODEX_EMAIL (optional)

        Numbered credentials (N):
        - OPENAI_CODEX_N_ACCESS_TOKEN
        - OPENAI_CODEX_N_REFRESH_TOKEN
        - OPENAI_CODEX_N_EXPIRY_DATE (optional)
        - OPENAI_CODEX_N_ID_TOKEN (optional)
        - OPENAI_CODEX_N_ACCOUNT_ID (optional)
        - OPENAI_CODEX_N_EMAIL (optional)
        """
        if credential_index and credential_index != "0":
            prefix = f"OPENAI_CODEX_{credential_index}"
            default_email = f"env-user-{credential_index}"
            env_index = credential_index
        else:
            prefix = "OPENAI_CODEX"
            default_email = "env-user"
            env_index = "0"

        access_token = os.getenv(f"{prefix}_ACCESS_TOKEN")
        refresh_token = os.getenv(f"{prefix}_REFRESH_TOKEN")

        if not (access_token and refresh_token):
            return None

        expiry_raw = os.getenv(f"{prefix}_EXPIRY_DATE", "")
        expiry_date: Optional[int] = None
        if expiry_raw:
            try:
                expiry_date = int(float(expiry_raw))
            except ValueError:
                lib_logger.warning(f"Invalid {prefix}_EXPIRY_DATE: {expiry_raw}")

        id_token = os.getenv(f"{prefix}_ID_TOKEN")
        account_id = os.getenv(f"{prefix}_ACCOUNT_ID")
        email = os.getenv(f"{prefix}_EMAIL")

        creds: Dict[str, Any] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "token_uri": TOKEN_ENDPOINT,
            "expiry_date": expiry_date or 0,
            "_proxy_metadata": {
                "email": email or default_email,
                "account_id": account_id,
                "last_check_timestamp": time.time(),
                "loaded_from_env": True,
                "env_credential_index": env_index,
            },
        }

        # Fill missing metadata/expiry from JWT claims
        self._populate_metadata_from_tokens(creds)

        # If expiry still missing, set conservative short expiry to trigger refresh soon
        if not creds.get("expiry_date"):
            creds["expiry_date"] = int((time.time() + 300) * 1000)

        return creds

    async def _read_creds_from_file(self, path: str) -> Dict[str, Any]:
        """Read credentials from disk and update cache."""
        try:
            with open(path, "r") as f:
                creds = json.load(f)

            if not isinstance(creds, dict):
                raise ValueError("Credential file root must be a JSON object")

            creds = self._ensure_proxy_metadata(creds)
            self._credentials_cache[path] = creds
            return creds

        except FileNotFoundError:
            raise IOError(f"OpenAI Codex credential file not found at '{path}'")
        except Exception as e:
            raise IOError(
                f"Failed to load OpenAI Codex credentials from '{path}': {e}"
            )

    async def _load_credentials(self, path: str) -> Dict[str, Any]:
        """Load credentials from cache, env, or file."""
        if path in self._credentials_cache:
            return self._credentials_cache[path]

        async with await self._get_lock(path):
            if path in self._credentials_cache:
                return self._credentials_cache[path]

            credential_index = self._parse_env_credential_path(path)
            if credential_index is not None:
                env_creds = self._load_from_env(credential_index)
                if env_creds:
                    self._credentials_cache[path] = env_creds
                    lib_logger.info(
                        f"Using OpenAI Codex env credential index {credential_index}"
                    )
                    return env_creds
                raise IOError(
                    f"Environment variables for OpenAI Codex credential index {credential_index} not found"
                )

            # File-based path, with legacy env fallback for backwards compatibility
            try:
                return await self._read_creds_from_file(path)
            except IOError:
                env_creds = self._load_from_env("0")
                if env_creds:
                    self._credentials_cache[path] = env_creds
                    lib_logger.info(
                        f"File '{path}' not found; using legacy OPENAI_CODEX_* environment credentials"
                    )
                    return env_creds
                raise

    async def _save_credentials(self, path: str, creds: Dict[str, Any]) -> bool:
        """
        Save credentials to disk, then update cache.

        Critical semantics:
        - For rotating refresh tokens, disk write must succeed before cache update.
        - Env-backed creds skip disk writes and update in-memory cache only.
        """
        creds = self._ensure_proxy_metadata(copy.deepcopy(creds))

        loaded_from_env = creds.get("_proxy_metadata", {}).get("loaded_from_env", False)
        if loaded_from_env or self._parse_env_credential_path(path) is not None:
            self._credentials_cache[path] = creds
            lib_logger.debug(
                f"OpenAI Codex credential '{path}' is env-backed; skipping disk write"
            )
            return True

        if not safe_write_json(
            path,
            creds,
            lib_logger,
            secure_permissions=True,
            buffer_on_failure=False,
        ):
            lib_logger.error(
                f"Failed to persist OpenAI Codex credentials for '{Path(path).name}'. Cache not updated."
            )
            return False

        self._credentials_cache[path] = creds
        return True

    # =========================================================================
    # Expiry / refresh helpers
    # =========================================================================

    def _is_token_expired(self, creds: Dict[str, Any]) -> bool:
        """Proactive expiry check using refresh buffer."""
        expiry_timestamp = float(creds.get("expiry_date", 0)) / 1000
        return expiry_timestamp < time.time() + REFRESH_EXPIRY_BUFFER_SECONDS

    def _is_token_truly_expired(self, creds: Dict[str, Any]) -> bool:
        """Strict expiry check without proactive buffer."""
        expiry_timestamp = float(creds.get("expiry_date", 0)) / 1000
        return expiry_timestamp < time.time()

    async def _exchange_code_for_tokens(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> Dict[str, Any]:
        """Exchange OAuth authorization code for tokens."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "LLM-API-Key-Proxy/OpenAICodex",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TOKEN_ENDPOINT, headers=headers, data=payload)
            response.raise_for_status()
            token_data = response.json()

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in")

        if not access_token or not refresh_token or not isinstance(expires_in, (int, float)):
            raise ValueError("Token exchange response missing required fields")

        return token_data

    async def _refresh_token(self, path: str, force: bool = False) -> Dict[str, Any]:
        """Refresh access token using refresh_token with retry/backoff."""
        async with await self._get_lock(path):
            cached_creds = self._credentials_cache.get(path)
            if not force and cached_creds and not self._is_token_expired(cached_creds):
                return cached_creds

            # Always load freshest source before refresh attempt
            is_env = self._parse_env_credential_path(path) is not None
            if is_env:
                source_creds = copy.deepcopy(await self._load_credentials(path))
            else:
                await self._read_creds_from_file(path)
                source_creds = copy.deepcopy(self._credentials_cache[path])

            refresh_token = source_creds.get("refresh_token")
            if not refresh_token:
                raise ValueError("No refresh_token found in OpenAI Codex credentials")

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "LLM-API-Key-Proxy/OpenAICodex",
            }

            max_retries = 3
            token_data = None
            last_error: Optional[Exception] = None

            async with httpx.AsyncClient(timeout=30.0) as client:
                for attempt in range(max_retries):
                    try:
                        response = await client.post(
                            TOKEN_ENDPOINT,
                            headers=headers,
                            data={
                                "grant_type": "refresh_token",
                                "refresh_token": refresh_token,
                                "client_id": CLIENT_ID,
                            },
                        )
                        response.raise_for_status()
                        token_data = response.json()
                        break

                    except httpx.HTTPStatusError as e:
                        last_error = e
                        status_code = e.response.status_code

                        error_type = ""
                        error_desc = ""
                        try:
                            payload = e.response.json()
                            error_type = payload.get("error", "")
                            error_desc = payload.get("error_description", "") or payload.get(
                                "message", ""
                            )
                        except Exception:
                            error_desc = e.response.text

                        # invalid_grant and authorization failures should trigger re-auth queue
                        if status_code == 400:
                            if (
                                error_type == "invalid_grant"
                                or "invalid_grant" in error_desc.lower()
                                or "invalid" in error_desc.lower()
                            ):
                                asyncio.create_task(
                                    self._queue_refresh(path, force=True, needs_reauth=True)
                                )
                                raise CredentialNeedsReauthError(
                                    credential_path=path,
                                    message=(
                                        f"OpenAI Codex refresh token invalid for '{Path(path).name}'. Re-auth queued."
                                    ),
                                )
                            raise

                        if status_code in (401, 403):
                            asyncio.create_task(
                                self._queue_refresh(path, force=True, needs_reauth=True)
                            )
                            raise CredentialNeedsReauthError(
                                credential_path=path,
                                message=(
                                    f"OpenAI Codex credential '{Path(path).name}' unauthorized (HTTP {status_code}). Re-auth queued."
                                ),
                            )

                        if status_code == 429:
                            retry_after = e.response.headers.get("Retry-After", "60")
                            try:
                                wait_seconds = max(1, int(float(retry_after)))
                            except ValueError:
                                wait_seconds = 60

                            if attempt < max_retries - 1:
                                await asyncio.sleep(wait_seconds)
                                continue
                            raise

                        if 500 <= status_code < 600:
                            if attempt < max_retries - 1:
                                await asyncio.sleep(2**attempt)
                                continue
                            raise

                        raise

                    except (httpx.RequestError, httpx.TimeoutException) as e:
                        last_error = e
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2**attempt)
                            continue
                        raise

            if token_data is None:
                self._refresh_failures[path] = self._refresh_failures.get(path, 0) + 1
                backoff_seconds = min(300, 30 * (2 ** self._refresh_failures[path]))
                self._next_refresh_after[path] = time.time() + backoff_seconds
                raise last_error or Exception("OpenAI Codex token refresh failed")

            access_token = token_data.get("access_token")
            if not access_token:
                raise ValueError("Refresh response missing access_token")

            expires_in = token_data.get("expires_in")
            if not isinstance(expires_in, (int, float)):
                raise ValueError("Refresh response missing expires_in")

            # Build UPDATED credential object (do not mutate cached source in-place)
            updated_creds = copy.deepcopy(source_creds)
            updated_creds["access_token"] = access_token
            updated_creds["refresh_token"] = token_data.get(
                "refresh_token", updated_creds.get("refresh_token")
            )

            if token_data.get("id_token"):
                updated_creds["id_token"] = token_data.get("id_token")

            updated_creds["expiry_date"] = int((time.time() + float(expires_in)) * 1000)
            updated_creds["token_uri"] = TOKEN_ENDPOINT

            self._ensure_proxy_metadata(updated_creds)

            if not updated_creds.get("access_token") or not updated_creds.get(
                "refresh_token"
            ):
                raise ValueError("Refreshed credentials missing required token fields")

            # Successful refresh clears backoff tracking
            self._refresh_failures.pop(path, None)
            self._next_refresh_after.pop(path, None)

            # Persist before mutating shared cache state
            if not await self._save_credentials(path, updated_creds):
                raise IOError(
                    f"Failed to persist refreshed OpenAI Codex credential '{Path(path).name}'"
                )

            return self._credentials_cache[path]

    # =========================================================================
    # Interactive OAuth flow
    # =========================================================================

    async def _perform_interactive_oauth(
        self,
        path: Optional[str],
        creds: Dict[str, Any],
        display_name: str,
    ) -> Dict[str, Any]:
        """Perform interactive OpenAI Codex OAuth authorization code flow with PKCE."""
        is_headless = is_headless_environment()

        # PKCE verifier/challenge (base64url, no padding)
        code_verifier = (
            base64.urlsafe_b64encode(secrets.token_bytes(32))
            .decode("utf-8")
            .rstrip("=")
        )
        code_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode("utf-8")).digest()
            )
            .decode("utf-8")
            .rstrip("=")
        )
        state = secrets.token_hex(32)

        callback_port = get_callback_port()
        redirect_uri = f"http://localhost:{callback_port}{CALLBACK_PATH}"

        auth_params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "pi",
        }
        auth_url = f"{AUTHORIZATION_ENDPOINT}?{urlencode(auth_params)}"

        callback_server = OAuthCallbackServer(port=callback_port)

        try:
            await callback_server.start(expected_state=state)

            if is_headless:
                help_text = Text.from_markup(
                    "Running in headless environment.\n"
                    "Open the URL below in a browser on another machine and complete login."
                )
            else:
                help_text = Text.from_markup(
                    "Open the URL below, complete sign-in, and return here."
                )

            console.print(
                Panel(
                    help_text,
                    title=f"OpenAI Codex OAuth Setup for [bold yellow]{display_name}[/bold yellow]",
                    style="bold blue",
                )
            )
            escaped_url = rich_escape(auth_url)
            console.print(f"[bold]URL:[/bold] [link={auth_url}]{escaped_url}[/link]\n")

            if not is_headless:
                try:
                    webbrowser.open(auth_url)
                    lib_logger.info("Browser opened for OpenAI Codex OAuth flow")
                except Exception as e:
                    lib_logger.warning(
                        f"Failed to auto-open browser for OpenAI Codex OAuth: {e}"
                    )

            code = await callback_server.wait_for_callback(
                timeout=float(self._reauth_timeout_seconds)
            )

            token_data = await self._exchange_code_for_tokens(
                code=code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
            )

            # Build updated credential object
            updated_creds = copy.deepcopy(creds)
            metadata = updated_creds.setdefault("_proxy_metadata", {})
            loaded_from_env = metadata.get("loaded_from_env", False)
            env_index = metadata.get("env_credential_index")

            updated_creds.update(
                {
                    "access_token": token_data.get("access_token"),
                    "refresh_token": token_data.get("refresh_token"),
                    "id_token": token_data.get("id_token"),
                    "token_uri": TOKEN_ENDPOINT,
                    "expiry_date": int(
                        (time.time() + float(token_data.get("expires_in", 3600))) * 1000
                    ),
                }
            )

            # Restore env metadata flags if this credential originated from env
            updated_creds.setdefault("_proxy_metadata", {})
            updated_creds["_proxy_metadata"]["loaded_from_env"] = loaded_from_env
            updated_creds["_proxy_metadata"]["env_credential_index"] = env_index

            self._ensure_proxy_metadata(updated_creds)

            if path:
                if not await self._save_credentials(path, updated_creds):
                    raise IOError(
                        f"Failed to save OpenAI Codex OAuth credentials for '{display_name}'"
                    )
            else:
                # in-memory setup flow
                creds.clear()
                creds.update(updated_creds)

            lib_logger.info(
                f"OpenAI Codex OAuth initialized successfully for '{display_name}'"
            )
            return updated_creds

        finally:
            await callback_server.stop()

    async def initialize_token(
        self,
        creds_or_path: Union[Dict[str, Any], str],
        force_interactive: bool = False,
    ) -> Dict[str, Any]:
        """
        Initialize OAuth token, refreshing or running interactive flow as needed.

        Interactive re-auth is globally coordinated via ReauthCoordinator so only
        one flow runs at a time across all providers.
        """
        path = creds_or_path if isinstance(creds_or_path, str) else None

        if isinstance(creds_or_path, dict):
            display_name = creds_or_path.get("_proxy_metadata", {}).get(
                "display_name", "in-memory OpenAI Codex credential"
            )
        else:
            display_name = Path(path).name if path else "in-memory OpenAI Codex credential"

        try:
            creds = (
                await self._load_credentials(creds_or_path) if path else copy.deepcopy(creds_or_path)
            )

            reason = ""
            if force_interactive:
                reason = "interactive re-auth explicitly requested"
            elif not creds.get("refresh_token"):
                reason = "refresh token is missing"
            elif self._is_token_expired(creds):
                reason = "token is expired"

            if reason:
                # Prefer non-interactive refresh when we have a refresh token and this is simple expiry
                if reason == "token is expired" and creds.get("refresh_token") and path:
                    try:
                        return await self._refresh_token(path)
                    except CredentialNeedsReauthError:
                        # Explicitly fall through into interactive re-auth path
                        pass
                    except Exception as e:
                        lib_logger.warning(
                            f"Automatic OpenAI Codex token refresh failed for '{display_name}': {e}. Falling back to interactive login."
                        )

                coordinator = get_reauth_coordinator()

                async def _do_interactive_oauth():
                    return await self._perform_interactive_oauth(path, creds, display_name)

                result = await coordinator.execute_reauth(
                    credential_path=path or display_name,
                    provider_name="OPENAI_CODEX",
                    reauth_func=_do_interactive_oauth,
                    timeout=float(self._reauth_timeout_seconds),
                )

                # Persist cache when path-based
                if path and isinstance(result, dict):
                    self._credentials_cache[path] = self._ensure_proxy_metadata(result)

                return result

            # Token is already valid
            creds = self._ensure_proxy_metadata(creds)
            if path:
                self._credentials_cache[path] = creds
            return creds

        except Exception as e:
            raise ValueError(
                f"Failed to initialize OpenAI Codex OAuth credential '{display_name}': {e}"
            )

    async def get_auth_header(self, credential_identifier: str) -> Dict[str, str]:
        creds = await self._load_credentials(credential_identifier)
        if self._is_token_expired(creds):
            creds = await self._refresh_token(credential_identifier)
        return {"Authorization": f"Bearer {creds['access_token']}"}

    async def get_user_info(
        self, creds_or_path: Union[Dict[str, Any], str]
    ) -> Dict[str, Any]:
        """Retrieve user info from _proxy_metadata."""
        try:
            path = creds_or_path if isinstance(creds_or_path, str) else None
            creds = await self._load_credentials(path) if path else copy.deepcopy(creds_or_path)

            if path:
                await self.initialize_token(path)
                creds = await self._load_credentials(path)

            metadata = creds.get("_proxy_metadata", {})
            email = metadata.get("email")
            account_id = metadata.get("account_id")

            # Update timestamp in cache only (non-critical metadata)
            if path and "_proxy_metadata" in creds:
                creds["_proxy_metadata"]["last_check_timestamp"] = time.time()
                self._credentials_cache[path] = creds

            return {
                "email": email,
                "account_id": account_id,
            }
        except Exception as e:
            lib_logger.error(f"Failed to get OpenAI Codex user info: {e}")
            return {"email": None, "account_id": None}

    async def proactively_refresh(self, credential_identifier: str):
        """Queue proactive refresh for credentials near expiry."""
        try:
            creds = await self._load_credentials(credential_identifier)
        except IOError:
            return

        if self._is_token_expired(creds):
            await self._queue_refresh(
                credential_identifier,
                force=False,
                needs_reauth=False,
            )

    # =========================================================================
    # Queue + availability plumbing
    # =========================================================================

    async def _get_lock(self, path: str) -> asyncio.Lock:
        async with self._locks_lock:
            if path not in self._refresh_locks:
                self._refresh_locks[path] = asyncio.Lock()
            return self._refresh_locks[path]

    def is_credential_available(self, path: str) -> bool:
        """
        Check if credential is available for rotation.

        Unavailable when:
        - In re-auth queue
        - Truly expired (past actual expiry)
        """
        if path in self._unavailable_credentials:
            marked_time = self._unavailable_credentials.get(path)
            if marked_time is not None:
                now = time.time()
                if now - marked_time > self._unavailable_ttl_seconds:
                    lib_logger.warning(
                        f"OpenAI Codex credential '{Path(path).name}' stuck in re-auth queue for {int(now - marked_time)}s. Auto-cleaning stale entry."
                    )
                    self._unavailable_credentials.pop(path, None)
                    self._queued_credentials.discard(path)
                else:
                    return False

        creds = self._credentials_cache.get(path)
        if creds and self._is_token_truly_expired(creds):
            if path not in self._queued_credentials:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._queue_refresh(path, force=True, needs_reauth=False)
                    )
                except RuntimeError:
                    # No running event loop (e.g., sync context); caller can still
                    # trigger refresh through normal async request flow.
                    pass
            return False

        return True

    async def _ensure_queue_processor_running(self):
        if self._queue_processor_task is None or self._queue_processor_task.done():
            self._queue_processor_task = asyncio.create_task(self._process_refresh_queue())

    async def _ensure_reauth_processor_running(self):
        if self._reauth_processor_task is None or self._reauth_processor_task.done():
            self._reauth_processor_task = asyncio.create_task(self._process_reauth_queue())

    async def _queue_refresh(
        self,
        path: str,
        force: bool = False,
        needs_reauth: bool = False,
    ):
        """Queue credential for refresh or re-auth."""
        if not needs_reauth:
            now = time.time()
            backoff_until = self._next_refresh_after.get(path)
            if backoff_until and now < backoff_until:
                return

        async with self._queue_tracking_lock:
            if path in self._queued_credentials:
                return

            self._queued_credentials.add(path)

            if needs_reauth:
                self._unavailable_credentials[path] = time.time()
                await self._reauth_queue.put(path)
                await self._ensure_reauth_processor_running()
            else:
                await self._refresh_queue.put((path, force))
                await self._ensure_queue_processor_running()

    async def _process_refresh_queue(self):
        """Sequential background worker for normal refresh queue."""
        while True:
            path = None
            try:
                try:
                    path, force = await asyncio.wait_for(self._refresh_queue.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    async with self._queue_tracking_lock:
                        self._queue_retry_count.clear()
                    self._queue_processor_task = None
                    return

                try:
                    creds = self._credentials_cache.get(path)
                    if creds and not self._is_token_expired(creds):
                        self._queue_retry_count.pop(path, None)
                        continue

                    try:
                        async with asyncio.timeout(self._refresh_timeout_seconds):
                            await self._refresh_token(path, force=force)
                        self._queue_retry_count.pop(path, None)

                    except asyncio.TimeoutError:
                        await self._handle_refresh_failure(path, force, "timeout")

                    except httpx.HTTPStatusError as e:
                        status_code = e.response.status_code
                        needs_reauth = False

                        if status_code == 400:
                            try:
                                payload = e.response.json()
                                error_type = payload.get("error", "")
                                error_desc = payload.get("error_description", "")
                            except Exception:
                                error_type = ""
                                error_desc = str(e)

                            if (
                                error_type == "invalid_grant"
                                or "invalid_grant" in error_desc.lower()
                                or "invalid" in error_desc.lower()
                            ):
                                needs_reauth = True

                        elif status_code in (401, 403):
                            needs_reauth = True

                        if needs_reauth:
                            self._queue_retry_count.pop(path, None)
                            async with self._queue_tracking_lock:
                                self._queued_credentials.discard(path)
                            await self._queue_refresh(path, force=True, needs_reauth=True)
                        else:
                            await self._handle_refresh_failure(path, force, f"HTTP {status_code}")

                    except CredentialNeedsReauthError:
                        self._queue_retry_count.pop(path, None)
                        async with self._queue_tracking_lock:
                            self._queued_credentials.discard(path)
                        await self._queue_refresh(path, force=True, needs_reauth=True)

                    except Exception as e:
                        await self._handle_refresh_failure(path, force, str(e))

                finally:
                    async with self._queue_tracking_lock:
                        if (
                            path in self._queued_credentials
                            and self._queue_retry_count.get(path, 0) == 0
                        ):
                            self._queued_credentials.discard(path)
                    self._refresh_queue.task_done()

                await asyncio.sleep(self._refresh_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                lib_logger.error(f"Error in OpenAI Codex refresh queue processor: {e}")
                if path:
                    async with self._queue_tracking_lock:
                        self._queued_credentials.discard(path)

    async def _handle_refresh_failure(self, path: str, force: bool, error: str):
        retry_count = self._queue_retry_count.get(path, 0) + 1
        self._queue_retry_count[path] = retry_count

        if retry_count >= self._refresh_max_retries:
            lib_logger.error(
                f"OpenAI Codex refresh max retries reached for '{Path(path).name}' (last error: {error})."
            )
            self._queue_retry_count.pop(path, None)
            async with self._queue_tracking_lock:
                self._queued_credentials.discard(path)
            return

        lib_logger.warning(
            f"OpenAI Codex refresh failed for '{Path(path).name}' ({error}). Retry {retry_count}/{self._refresh_max_retries}."
        )
        await self._refresh_queue.put((path, force))

    async def _process_reauth_queue(self):
        """Sequential background worker for interactive re-auth queue."""
        while True:
            path = None
            try:
                try:
                    path = await asyncio.wait_for(self._reauth_queue.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    self._reauth_processor_task = None
                    return

                try:
                    lib_logger.info(
                        f"Starting OpenAI Codex interactive re-auth for '{Path(path).name}'"
                    )
                    await self.initialize_token(path, force_interactive=True)
                    lib_logger.info(
                        f"OpenAI Codex re-auth succeeded for '{Path(path).name}'"
                    )
                except Exception as e:
                    lib_logger.error(
                        f"OpenAI Codex re-auth failed for '{Path(path).name}': {e}"
                    )
                finally:
                    async with self._queue_tracking_lock:
                        self._queued_credentials.discard(path)
                        self._unavailable_credentials.pop(path, None)
                    self._reauth_queue.task_done()

            except asyncio.CancelledError:
                if path:
                    async with self._queue_tracking_lock:
                        self._queued_credentials.discard(path)
                        self._unavailable_credentials.pop(path, None)
                break
            except Exception as e:
                lib_logger.error(f"Error in OpenAI Codex re-auth queue processor: {e}")
                if path:
                    async with self._queue_tracking_lock:
                        self._queued_credentials.discard(path)
                        self._unavailable_credentials.pop(path, None)

    # =========================================================================
    # Credential management methods for credential_tool
    # =========================================================================

    def _get_provider_file_prefix(self) -> str:
        return "openai_codex"

    def _get_oauth_base_dir(self) -> Path:
        return Path.cwd() / "oauth_creds"

    def _find_existing_credential_by_identity(
        self,
        email: Optional[str],
        account_id: Optional[str],
        base_dir: Optional[Path] = None,
    ) -> Optional[Path]:
        if base_dir is None:
            base_dir = self._get_oauth_base_dir()

        prefix = self._get_provider_file_prefix()
        pattern = str(base_dir / f"{prefix}_oauth_*.json")

        for cred_file in glob(pattern):
            try:
                with open(cred_file, "r") as f:
                    creds = json.load(f)
                metadata = creds.get("_proxy_metadata", {})
                existing_email = metadata.get("email")
                existing_account_id = metadata.get("account_id")

                if email and existing_email and existing_email == email:
                    return Path(cred_file)
                if account_id and existing_account_id and existing_account_id == account_id:
                    return Path(cred_file)

            except Exception:
                continue

        return None

    def _get_next_credential_number(self, base_dir: Optional[Path] = None) -> int:
        if base_dir is None:
            base_dir = self._get_oauth_base_dir()

        prefix = self._get_provider_file_prefix()
        pattern = str(base_dir / f"{prefix}_oauth_*.json")

        existing_numbers = []
        for cred_file in glob(pattern):
            match = re.search(r"_oauth_(\d+)\.json$", cred_file)
            if match:
                existing_numbers.append(int(match.group(1)))

        return (max(existing_numbers) + 1) if existing_numbers else 1

    def _build_credential_path(
        self,
        base_dir: Optional[Path] = None,
        number: Optional[int] = None,
    ) -> Path:
        if base_dir is None:
            base_dir = self._get_oauth_base_dir()

        if number is None:
            number = self._get_next_credential_number(base_dir)

        filename = f"{self._get_provider_file_prefix()}_oauth_{number}.json"
        return base_dir / filename

    async def setup_credential(
        self,
        base_dir: Optional[Path] = None,
    ) -> OpenAICodexCredentialSetupResult:
        """Complete OpenAI Codex credential setup flow."""
        if base_dir is None:
            base_dir = self._get_oauth_base_dir()

        base_dir.mkdir(parents=True, exist_ok=True)

        try:
            temp_creds = {
                "_proxy_metadata": {
                    "display_name": "new OpenAI Codex credential",
                    "loaded_from_env": False,
                    "env_credential_index": None,
                }
            }
            new_creds = await self.initialize_token(temp_creds)

            metadata = new_creds.get("_proxy_metadata", {})
            email = metadata.get("email")
            account_id = metadata.get("account_id")

            existing_path = self._find_existing_credential_by_identity(
                email=email,
                account_id=account_id,
                base_dir=base_dir,
            )
            is_update = existing_path is not None
            file_path = existing_path if is_update else self._build_credential_path(base_dir)

            if not await self._save_credentials(str(file_path), new_creds):
                return OpenAICodexCredentialSetupResult(
                    success=False,
                    error=f"Failed to save OpenAI Codex credential to {file_path.name}",
                )

            return OpenAICodexCredentialSetupResult(
                success=True,
                file_path=str(file_path),
                email=email,
                is_update=is_update,
                credentials=new_creds,
            )

        except Exception as e:
            lib_logger.error(f"OpenAI Codex credential setup failed: {e}")
            return OpenAICodexCredentialSetupResult(success=False, error=str(e))

    def build_env_lines(self, creds: Dict[str, Any], cred_number: int) -> List[str]:
        """Build OPENAI_CODEX_N_* env lines from credential JSON."""
        metadata = creds.get("_proxy_metadata", {})
        email = metadata.get("email", "unknown")
        account_id = metadata.get("account_id", "")

        prefix = f"OPENAI_CODEX_{cred_number}"

        lines = [
            f"# OPENAI_CODEX Credential #{cred_number} for: {email}",
            f"# Exported from: openai_codex_oauth_{cred_number}.json",
            f"# Generated at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"{prefix}_ACCESS_TOKEN={creds.get('access_token', '')}",
            f"{prefix}_REFRESH_TOKEN={creds.get('refresh_token', '')}",
            f"{prefix}_EXPIRY_DATE={int(float(creds.get('expiry_date', 0)))}",
            f"{prefix}_ID_TOKEN={creds.get('id_token', '')}",
            f"{prefix}_ACCOUNT_ID={account_id}",
            f"{prefix}_EMAIL={email}",
        ]

        return lines

    def export_credential_to_env(
        self,
        credential_path: str,
        output_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Export a credential JSON file to .env format."""
        try:
            cred_path = Path(credential_path)
            with open(cred_path, "r") as f:
                creds = json.load(f)

            metadata = creds.get("_proxy_metadata", {})
            email = metadata.get("email", "unknown")

            match = re.search(r"_oauth_(\d+)\.json$", cred_path.name)
            cred_number = int(match.group(1)) if match else 1

            if output_dir is None:
                output_dir = cred_path.parent

            safe_email = str(email).replace("@", "_at_").replace(".", "_")
            env_filename = f"openai_codex_{cred_number}_{safe_email}.env"
            env_path = output_dir / env_filename

            env_lines = self.build_env_lines(creds, cred_number)
            with open(env_path, "w") as f:
                f.write("\n".join(env_lines))

            lib_logger.info(f"Exported OpenAI Codex credential to {env_path}")
            return str(env_path)

        except Exception as e:
            lib_logger.error(f"Failed to export OpenAI Codex credential: {e}")
            return None

    def list_credentials(self, base_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
        """List all local OpenAI Codex credential files."""
        if base_dir is None:
            base_dir = self._get_oauth_base_dir()

        prefix = self._get_provider_file_prefix()
        pattern = str(base_dir / f"{prefix}_oauth_*.json")

        credentials: List[Dict[str, Any]] = []
        for cred_file in sorted(glob(pattern)):
            try:
                with open(cred_file, "r") as f:
                    creds = json.load(f)

                metadata = creds.get("_proxy_metadata", {})
                match = re.search(r"_oauth_(\d+)\.json$", cred_file)
                number = int(match.group(1)) if match else 0

                credentials.append(
                    {
                        "file_path": cred_file,
                        "email": metadata.get("email", "unknown"),
                        "account_id": metadata.get("account_id"),
                        "number": number,
                    }
                )
            except Exception:
                continue

        return credentials

    def delete_credential(self, credential_path: str) -> bool:
        """Delete an OpenAI Codex credential file."""
        try:
            cred_path = Path(credential_path)
            prefix = self._get_provider_file_prefix()

            if not cred_path.name.startswith(f"{prefix}_oauth_"):
                lib_logger.error(
                    f"File {cred_path.name} does not appear to be an OpenAI Codex credential"
                )
                return False

            if not cred_path.exists():
                lib_logger.warning(
                    f"OpenAI Codex credential file does not exist: {credential_path}"
                )
                return False

            self._credentials_cache.pop(credential_path, None)
            cred_path.unlink()
            lib_logger.info(f"Deleted OpenAI Codex credential file: {credential_path}")
            return True

        except Exception as e:
            lib_logger.error(f"Failed to delete OpenAI Codex credential: {e}")
            return False
