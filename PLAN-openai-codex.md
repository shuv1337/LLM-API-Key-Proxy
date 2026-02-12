# PLAN: OpenAI Codex OAuth + Multi-Account Support (Revised)

## Goal
Add first-class `openai_codex` support to LLM-API-Key-Proxy with:
- OAuth login + token refresh
- file/env credential loading
- multi-account rotation via existing `UsageManager`
- OpenAI-compatible `/v1/chat/completions` served through Codex Responses backend
- first-run import from existing Codex CLI credentials (`~/.codex/auth.json`, `~/.codex-accounts.json`)

---

## Review updates applied in this revision

- Aligned with current local-first architecture: **local managed creds stay in `oauth_creds/`**, not `~/.openai_codex`.
- Reduced MVP risk: **no cross-provider OAuth base refactor in phase 1**.
- Added protocol validation gate (headers/endpoints/SSE event taxonomy) before implementation.
- Expanded wiring checklist to all known hardcoded OAuth provider lists (credential tool, launcher TUI, settings tool).
- Added explicit `env://openai_codex/N` parity requirements and test-harness bootstrap work.

---

## 0) Scope decisions + preflight validation (must lock before coding)

### 0.1 Provider identity and defaults

- [x] Provider key: `openai_codex`
- [x] OAuth env prefix: `OPENAI_CODEX`
- [x] Default API base: `https://chatgpt.com/backend-api`
- [x] Responses endpoint path: `/codex/responses`
- [x] Default rotation mode for provider: `sequential`
- [x] Callback env var: `OPENAI_CODEX_OAUTH_PORT`
- [x] JWT parsing strategy: unverified base64url decode (no `PyJWT` dependency)

### 0.2 Architecture alignment (critical)

- [x] Keep **local managed credentials** in project data dir: `oauth_creds/openai_codex_oauth_N.json`
  - [x] Match existing patterns in `src/rotator_library/utils/paths.py` and other auth bases
  - [x] Do **not** introduce a new default managed dir under `~/.openai_codex` for MVP
- [x] Treat `~/.codex/*` only as **import source**, never as primary writable store

### 0.3 Protocol truth capture (before implementation)

- [x] Capture one successful non-stream + stream Codex call and confirm:
  - [x] Auth endpoint(s) and token exchange params
  - [x] Required request headers (`chatgpt-account-id`, `OpenAI-Beta`, `originator`, etc.)
  - [x] SSE event names/payload shapes
  - [x] Error body format for 401/403/429/5xx
- [x] Save representative payloads/events as test fixtures under `tests/fixtures/openai_codex/`

---

## 1) OAuth + credential plumbing

## 1.1 Add OpenAI Codex auth base (MVP approach: provider-specific class)

- [x] Create `src/rotator_library/providers/openai_codex_auth_base.py`
- [x] Base implementation strategy for MVP:
  - [x] Adapt proven queue/refresh/reauth approach from `qwen_auth_base.py` / `iflow_auth_base.py`
  - [x] **Do not** refactor `GoogleOAuthBase` or create shared `oauth_base.py` in phase 1

### 1.1.1 Core lifecycle and queue infrastructure

- [x] Implement credential cache/locking/queue internals:
  - [x] `_credentials_cache`, `_load_credentials()`, `_save_credentials()`
  - [x] `_refresh_locks`, `_locks_lock`, `_get_lock()`
  - [x] `_refresh_queue`, `_reauth_queue`
  - [x] `_queue_refresh()`, `_process_refresh_queue()`, `_process_reauth_queue()`
  - [x] `_refresh_failures`, `_next_refresh_after` (backoff tracking)
  - [x] `_queued_credentials`, `_unavailable_credentials`, TTL cleanup
- [x] Implement `is_credential_available(path)` with:
  - [x] re-auth queue exclusion
  - [x] true-expiry check (not proactive buffer)
- [x] Implement `proactively_refresh(credential_identifier)` queue-based behavior

### 1.1.2 OAuth flow and refresh behavior

- [x] Interactive OAuth with PKCE + state
  - [x] Local callback: `http://localhost:{OPENAI_CODEX_OAUTH_PORT}/oauth2callback`
  - [x] `ReauthCoordinator` integration (single interactive flow globally)
- [x] Token exchange endpoint: `https://auth.openai.com/oauth/token`
- [x] Authorization endpoint: `https://auth.openai.com/oauth/authorize`
- [x] Refresh flow (`grant_type=refresh_token`) with retry/backoff (3 attempts)
- [x] Refresh error handling:
  - [x] `400 invalid_grant` => queue re-auth + raise `CredentialNeedsReauthError`
  - [x] `401/403` => queue re-auth + raise `CredentialNeedsReauthError`
  - [x] `429` => honor `Retry-After`
  - [x] `5xx` => exponential backoff retry

### 1.1.3 Safe persistence semantics (critical)

- [x] `_save_credentials()` uses `safe_write_json(..., secure_permissions=True)`
- [x] For rotating refresh-token safety:
  - [x] Write-to-disk success required before cache mutation for refreshed tokens
  - [x] Avoid stale-cache overwrite scenarios
- [x] Env-backed credentials (`_proxy_metadata.loaded_from_env=true`) skip disk writes safely

### 1.1.4 JWT and metadata extraction

- [x] Add unverified JWT decode helper (base64url payload decode with padding)
- [x] Extract from access token (fallback to `id_token`):
  - [x] `account_id` claim: `https://api.openai.com/auth.chatgpt_account_id`
  - [x] email claim fallback chain: `email` -> `sub`
  - [x] `exp` for token expiry
- [x] Maintain metadata under `_proxy_metadata`:
  - [x] `email`, `account_id`, `last_check_timestamp`
  - [x] `loaded_from_env`, `env_credential_index`

### 1.1.5 Env credential support

- [x] Support both formats in `_load_from_env()`:
  - [x] legacy single: `OPENAI_CODEX_ACCESS_TOKEN`, `OPENAI_CODEX_REFRESH_TOKEN`, ...
  - [x] numbered: `OPENAI_CODEX_1_ACCESS_TOKEN`, `OPENAI_CODEX_1_REFRESH_TOKEN`, ...
- [x] Implement `_parse_env_credential_path(path)` for `env://openai_codex/N`
- [x] Ensure `_load_credentials()` works for file paths **and** `env://` virtual paths

### 1.1.6 Public methods expected by tooling/runtime

- [x] `setup_credential()`
- [x] `initialize_token(path_or_creds, force_interactive=False)`
- [x] `get_user_info(creds_or_path)`
- [x] `get_auth_header(credential_identifier)`
- [x] `list_credentials(base_dir)`
- [x] `delete_credential(path)`
- [x] `build_env_lines(creds, cred_number)`
- [x] `export_credential_to_env(credential_path, base_dir)` (used by credential tool export flows)
- [x] `_get_provider_file_prefix() -> "openai_codex"`

### 1.1.7 Credential schema (`openai_codex_oauth_N.json`)

```json
{
  "access_token": "eyJhbGciOi...",
  "refresh_token": "rt_...",
  "id_token": "eyJhbGciOi...",
  "expiry_date": 1739400000000,
  "token_uri": "https://auth.openai.com/oauth/token",
  "_proxy_metadata": {
    "email": "user@example.com",
    "account_id": "acct_...",
    "last_check_timestamp": 1739396400.0,
    "loaded_from_env": false,
    "env_credential_index": null
  }
}
```

> Note: client metadata like `client_id` should be class constants unless Codex token refresh explicitly requires persisted values.

---

## 1.2 First-run import from Codex CLI credentials (CredentialManager integration)

- [x] Update `src/rotator_library/credential_manager.py` to add Codex import helper
  - [x] Trigger only when:
    - [x] provider is `openai_codex`
    - [x] no local `oauth_creds/openai_codex_oauth_*.json`
    - [x] no env-based OpenAI Codex credentials already selected
- [x] Import sources (read-only):
  - [x] `~/.codex/auth.json` (single account)
  - [x] `~/.codex-accounts.json` (multi-account)
- [x] Normalize imported records to proxy schema
- [x] Extract and store `account_id` + email from JWT claims during import
- [x] Skip malformed entries gracefully with warnings
- [x] Preserve original source files untouched
- [x] Log import summary (count + identifiers)

---

## 1.3 Wire registries and discovery maps

- [x] Update `src/rotator_library/provider_factory.py`
  - [x] Import `OpenAICodexAuthBase`
  - [x] Add `"openai_codex": OpenAICodexAuthBase` to `PROVIDER_MAP`
- [x] Update `src/rotator_library/credential_manager.py`
  - [x] Add to `DEFAULT_OAUTH_DIRS`: `"openai_codex": Path.home() / ".codex"` (source import context)
  - [x] Add to `ENV_OAUTH_PROVIDERS`: `"openai_codex": "OPENAI_CODEX"`

---

## 1.4 Wire credential UI, launcher UI, and settings UI

### 1.4.1 Credential tool updates (`src/rotator_library/credential_tool.py`)

- [x] Add to `OAUTH_FRIENDLY_NAMES`: `"openai_codex": "OpenAI Codex"`
- [x] Add to OAuth provider lists:
  - [x] `_get_oauth_credentials_summary()` hardcoded list
  - [x] `combine_all_credentials()` hardcoded list
- [x] Add to OAuth-only exclusions in API-key flow:
  - [x] `oauth_only_providers` in `setup_api_key()`
- [x] Add to setup display mapping in `setup_new_credential()`
- [x] Export support:
  - [x] Add OpenAI Codex export option(s) or refactor export menu to provider-driven generic flow
  - [x] Ensure combine/export features call new auth-base methods

### 1.4.2 Launcher TUI updates (`src/proxy_app/launcher_tui.py`)

- [x] Add `"openai_codex": "OPENAI_CODEX"` to `env_oauth_providers` in `SettingsDetector.detect_credentials()`

### 1.4.3 Settings tool updates (`src/proxy_app/settings_tool.py`)

- [x] Import Codex default callback port from auth class with fallback constant
- [x] Add provider settings block for `openai_codex`:
  - [x] `OPENAI_CODEX_OAUTH_PORT`
- [x] Register `openai_codex` in `PROVIDER_SETTINGS_MAP`

---

## 1.5 Provider plugin auto-registration verification

- [x] Create `src/rotator_library/providers/openai_codex_provider.py`
  - [x] Confirm `providers/__init__.py` auto-registers as `openai_codex`
- [x] Verify name consistency across all maps/lists:
  - [x] `PROVIDER_MAP` (`provider_factory.py`)
  - [x] `DEFAULT_OAUTH_DIRS` / `ENV_OAUTH_PROVIDERS` (`credential_manager.py`)
  - [x] `OAUTH_FRIENDLY_NAMES` + hardcoded OAuth lists (`credential_tool.py`)
  - [x] `env_oauth_providers` (`launcher_tui.py`)
  - [x] `PROVIDER_SETTINGS_MAP` (`settings_tool.py`)

---

## 2) Codex inference provider (`openai_codex_provider.py`)

## 2.1 Provider class skeleton

- [x] Implement `OpenAICodexProvider(OpenAICodexAuthBase, ProviderInterface)`
- [x] Set class behavior:
  - [x] `has_custom_logic() -> True`
  - [x] `skip_cost_calculation = True`
  - [x] `default_rotation_mode = "sequential"`
  - [x] `provider_env_name = "openai_codex"`
- [x] `get_models()` model source order:
  - [x] `OPENAI_CODEX_MODELS` via `ModelDefinitions` (priority)
  - [x] hardcoded sane fallback models
  - [x] optional dynamic discovery if Codex endpoint supports model listing

## 2.2 Credential initialization + metadata cache

- [x] Implement `initialize_credentials(credential_paths)` startup hook:
  - [x] preload credentials (file + `env://`)
  - [x] validate expiry and queue refresh where needed
  - [x] parse/cache `account_id` and email
  - [x] log summary of ready/refreshing/reauth-required credentials

## 2.3 Non-streaming completion path

- [x] Implement `acompletion()` for `stream=false`
- [x] Credential handling:
  - [x] use `credential_identifier` from client
  - [x] support file + `env://` paths consistently (no `os.path.isfile` shortcut assumptions)
  - [x] ensure `initialize_token()` called before request when needed
- [x] Transform incoming OpenAI chat payload to Codex Responses payload:
  - [x] `messages` -> Codex `input`
  - [x] `model`, `temperature`, `top_p`, `max_tokens`
  - [x] tools/tool_choice mapping where supported
- [x] Request target:
  - [x] `POST ${OPENAI_CODEX_API_BASE or default}/codex/responses`
- [x] Required headers:
  - [x] `Authorization: Bearer <access_token>`
  - [x] `chatgpt-account-id: <account_id>`
  - [x] protocol-validated beta/originator headers from preflight
- [x] Parse response into `litellm.ModelResponse`

## 2.4 Streaming path + SSE translation

- [x] Implement dedicated SSE parser/translator
- [x] Handle expected Codex event families (validated from fixtures):
  - [x] `response.created`
  - [x] `response.output_item.added`
  - [x] `response.content_part.added`
  - [x] `response.content_part.delta`
  - [x] `response.content_part.done`
  - [x] `response.output_item.done`
  - [x] `response.completed`
  - [x] `response.failed` / `response.incomplete`
  - [x] `error`
- [x] Tool-call delta mapping:
  - [x] `response.function_call_arguments.delta`
  - [x] `response.function_call_arguments.done`
- [x] Emit translated `litellm.ModelResponse` chunks (not raw SSE strings)
  - [x] compatible with `RotatingClient._safe_streaming_wrapper()`
- [x] Finish reason mapping:
  - [x] stop -> `stop`
  - [x] max_output_tokens -> `length`
  - [x] tool_calls -> `tool_calls`
  - [x] content_filter -> `content_filter`
- [x] Usage extraction from terminal event:
  - [x] `input_tokens` -> `usage.prompt_tokens`
  - [x] `output_tokens` -> `usage.completion_tokens`
  - [x] `total_tokens` -> `usage.total_tokens`
- [x] Unknown events:
  - [x] ignore safely with debug logs
  - [x] do not break stream unless terminal error condition

## 2.5 Error classification + rotation compatibility

- [x] Ensure HTTP errors surface as `httpx.HTTPStatusError` (or equivalent classified exceptions)
- [x] Validate classification in existing `classify_error()` flow (`error_handler.py`):
  - [x] 401/403 => authentication/forbidden -> rotate credential
  - [x] 429 => rate_limit/quota_exceeded -> cooldown/rotate
  - [x] 5xx => server_error -> retry/rotate
  - [x] context-length style 400 => `context_window_exceeded`
- [x] Implement `@staticmethod parse_quota_error(error, error_body=None)` on provider
  - [x] parse `Retry-After`
  - [x] parse Codex-specific quota payload fields if present

## 2.6 Quota/tier placeholders (MVP-safe defaults)

- [x] Add conservative placeholders:
  - [x] `tier_priorities`
  - [x] `usage_reset_configs`
  - [x] `model_quota_groups`
- [x] Mark with TODOs for empirical tuning once real quota behavior is observed

---

## 3) Configuration + documentation updates

## 3.1 `.env.example`

- [x] Add one-time file import path:
  - [x] `OPENAI_CODEX_OAUTH_1`
- [x] Add stateless env credential vars (legacy + numbered):
  - [x] `OPENAI_CODEX_ACCESS_TOKEN`
  - [x] `OPENAI_CODEX_REFRESH_TOKEN`
  - [x] `OPENAI_CODEX_EXPIRY_DATE`
  - [x] `OPENAI_CODEX_ID_TOKEN`
  - [x] `OPENAI_CODEX_ACCOUNT_ID`
  - [x] `OPENAI_CODEX_EMAIL`
  - [x] `OPENAI_CODEX_1_*` variants
- [x] Add routing/config vars:
  - [x] `OPENAI_CODEX_API_BASE`
  - [x] `OPENAI_CODEX_OAUTH_PORT`
  - [x] `OPENAI_CODEX_MODELS`
  - [x] `ROTATION_MODE_OPENAI_CODEX`

## 3.2 `README.md`

- [x] Add OpenAI Codex to OAuth provider lists/tables
- [x] Add setup instructions:
  - [x] interactive OAuth flow
  - [x] first-run auto-import from `~/.codex/*`
  - [x] env-based stateless deployment format
- [x] Add callback-port table row for OpenAI Codex

## 3.3 `DOCUMENTATION.md`

- [x] Update credential discovery/import flow to include Codex source files
- [x] Add OpenAI Codex auth/provider architecture section
- [x] Document schema + env vars + runtime refresh/rotation behavior
- [x] Add troubleshooting section:
  - [x] malformed JWT payload
  - [x] missing account-id claim
  - [x] callback port conflicts
  - [x] header mismatch / 403 failures

---

## 4) Tests

## 4.0 Test harness bootstrap (repo currently has no test suite)

- [x] Add test directory structure: `tests/`
- [x] Add test dependencies (`pytest`, `pytest-asyncio`, `respx` or equivalent)
- [x] Add minimal test run documentation/command

## 4.1 Auth base tests (`tests/test_openai_codex_auth.py`)

- [x] JWT decode helper:
  - [x] valid token
  - [x] malformed token
  - [x] missing claims
- [x] expiry logic:
  - [x] `_is_token_expired()` with proactive buffer
  - [x] `_is_token_truly_expired()` strict expiry
- [x] env loading:
  - [x] legacy vars
  - [x] numbered vars
  - [x] `env://openai_codex/N` parsing
- [x] save/load round-trip with `_proxy_metadata`
- [x] re-auth queue availability behavior (`is_credential_available`)

## 4.2 Import tests (`tests/test_openai_codex_import.py`)

- [x] import from `~/.codex/auth.json` format
- [x] import from `~/.codex-accounts.json` format
- [x] skip import when local `openai_codex_oauth_*.json` exists
- [x] malformed source files handled gracefully
- [x] source files never modified

## 4.3 Provider request mapping tests (`tests/test_openai_codex_provider.py`)

- [x] chat request mapping to Codex Responses payload
- [x] non-stream response mapping to `ModelResponse`
- [x] header construction includes account-id + auth headers
- [x] env credential identifiers work (no file-only assumptions)

## 4.4 SSE translation tests (`tests/test_openai_codex_sse.py`)

- [x] fixture-driven event sequence -> expected chunk sequence
- [x] content deltas
- [x] tool-call deltas
- [x] finish reason mapping
- [x] usage extraction
- [x] error event propagation
- [x] unknown event tolerance

## 4.5 Wiring regression tests (lightweight)

- [x] credential discovery recognizes OpenAI Codex env vars
- [x] provider_factory returns OpenAICodexAuthBase
- [x] `providers` auto-registration includes `openai_codex`

---

## 5) Manual smoke-test checklist

- [x] `python -m rotator_library.credential_tool` shows **OpenAI Codex** in OAuth setup list
- [x] OpenAI Codex is excluded from API-key setup list (`oauth_only_providers`)
- [x] first run with no local creds imports from `~/.codex/*` into `oauth_creds/openai_codex_oauth_*.json`
- [x] env-based `env://openai_codex/N` credentials are detected and used
- [x] `/v1/models` includes `openai_codex/*` models
- [x] `/v1/chat/completions` works for:
  - [x] `stream=false`
  - [x] `stream=true`
- [x] expired token refresh works (proactive + on-demand)
- [x] invalid refresh token queues re-auth and rotates to next credential
- [x] `is_credential_available()` returns false for re-auth queued / truly expired creds
- [x] multi-account rotation works in:
  - [x] `sequential` (default)
  - [x] `balanced` (override)
- [x] launcher/settings UIs show Codex OAuth counts and callback-port setting correctly

---

## 6) Optional phase 2 (post-MVP)

- [ ] Extract common OAuth queue/cache logic into shared base mixin for `google_oauth_base`, `qwen_auth_base`, `iflow_auth_base`, and Codex
- [ ] Refactor credential tool OAuth provider lists/exports to dynamic provider-driven implementation
- [ ] Add `model_info_service` alias mapping for `openai_codex` if pricing/capability enrichment is desired
- [ ] Tune tier priorities/quota windows from observed production behavior
- [ ] Add periodic background reconciliation from external `~/.codex` stores if needed

---

## Proposed implementation order

1. **Protocol validation gate** — lock endpoints/headers/events from real fixtures
2. **Auth base** — `openai_codex_auth_base.py` (queue + refresh + reauth + env support)
3. **First-run import** — CredentialManager import flow for `~/.codex/*`
4. **Registry/discovery wiring** — provider_factory + credential_manager maps
5. **UI wiring** — credential_tool + launcher_tui + settings_tool
6. **Provider skeleton** — `openai_codex_provider.py`, model list, startup init
7. **Non-streaming completion** — request mapping + response mapping
8. **Streaming translator** — SSE event translation + tool calls + usage
9. **Error/quota integration** — `parse_quota_error`, retry/cooldown compatibility
10. **Tests** — harness + auth/import/provider/SSE/wiring tests
11. **Docs/config** — `.env.example`, `README.md`, `DOCUMENTATION.md`
12. **Manual smoke validation** — end-to-end checklist
