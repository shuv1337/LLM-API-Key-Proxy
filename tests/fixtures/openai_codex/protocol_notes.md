# OpenAI Codex protocol capture (2026-02-12)

Captured against `https://chatgpt.com/backend-api/codex/responses` using a valid Codex OAuth token from `~/.codex/auth.json`.

## OAuth

- Authorization endpoint: `https://auth.openai.com/oauth/authorize`
- Token endpoint: `https://auth.openai.com/oauth/token`
- Authorization code token exchange params:
  - `grant_type=authorization_code`
  - `client_id=app_EMoamEEZ73f0CkXaXp7hrann`
  - `redirect_uri=http://localhost:<OPENAI_CODEX_OAUTH_PORT>/oauth2callback`
  - `code_verifier=<pkce-verifier>`
- Refresh params:
  - `grant_type=refresh_token`
  - `refresh_token=<token>`
  - `client_id=app_EMoamEEZ73f0CkXaXp7hrann`

## Endpoint + request shape

- Endpoint: `POST /codex/responses`
- Requires `stream=true` (non-stream returns 400 with `{"detail":"Stream must be set to true"}`)
- Requires non-empty `instructions` (missing instructions returns 400 with `{"detail":"Instructions are required"}`)

Observed working request body fields:

- `model`
- `stream` (must be `true`)
- `store` (`false`)
- `instructions`
- `input` (Responses input format)
- `text.verbosity` (for `gpt-5.1-codex`, `low` was rejected; `medium` worked)
- `tool_choice`
- `parallel_tool_calls`

## Headers

Observed and/or validated for provider implementation:

- `Authorization: Bearer <access_token>`
- `chatgpt-account-id: <account_id>`
- `OpenAI-Beta: responses=experimental`
- `originator: pi`
- `Accept: text/event-stream`
- `Content-Type: application/json`

## SSE event taxonomy (observed)

- `response.created`
- `response.in_progress`
- `response.output_item.added`
- `response.output_item.done`
- `response.content_part.added`
- `response.output_text.delta`
- `response.output_text.done`
- `response.content_part.done`
- `response.completed`

Provider additionally supports planned aliases/events:

- `response.content_part.delta`
- `response.function_call_arguments.delta`
- `response.function_call_arguments.done`
- `response.incomplete`
- `response.failed`
- `error`

## Error body fixtures

- `error_missing_instructions.json`
- `error_stream_required.json`
- `error_unsupported_verbosity.json`
