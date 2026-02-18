import base64
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from rotator_library.providers.openai_codex_provider import OpenAICodexProvider


def _build_jwt(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}

    def b64url(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{b64url(header)}.{b64url(payload)}.sig"


def _build_sse_payload(text: str = "pong") -> bytes:
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp_1", "created_at": int(time.time()), "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "item": {
                "id": "msg_1",
                "type": "message",
                "status": "in_progress",
                "content": [],
                "role": "assistant",
            },
        },
        {
            "type": "response.content_part.added",
            "item_id": "msg_1",
            "part": {"type": "output_text", "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": text,
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 3,
                    "total_tokens": 8,
                },
            },
        },
    ]

    sse = "\n\n".join(f"data: {json.dumps(evt)}" for evt in events) + "\n\n"
    return sse.encode("utf-8")


@pytest.fixture
def provider() -> OpenAICodexProvider:
    return OpenAICodexProvider()


@pytest.fixture
def credential_file(tmp_path: Path) -> Path:
    payload = {
        "email": "provider@example.com",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_provider"},
    }

    cred_path = tmp_path / "openai_codex_oauth_1.json"
    cred_path.write_text(
        json.dumps(
            {
                "access_token": _build_jwt(payload),
                "refresh_token": "rt_provider",
                "id_token": _build_jwt(payload),
                "expiry_date": int((time.time() + 3600) * 1000),
                "token_uri": "https://auth.openai.com/oauth/token",
                "_proxy_metadata": {
                    "email": "provider@example.com",
                    "account_id": "acct_provider",
                    "last_check_timestamp": time.time(),
                    "loaded_from_env": False,
                    "env_credential_index": None,
                },
            },
            indent=2,
        )
    )
    return cred_path


def test_chat_request_mapping_to_codex_payload(provider: OpenAICodexProvider):
    payload = provider._build_codex_payload(
        model_name="gpt-5.1-codex",
        messages=[
            {"role": "system", "content": "System guidance"},
            {"role": "user", "content": "hello"},
        ],
        temperature=0.2,
        top_p=0.9,
        max_tokens=123,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Lookup data",
                    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            }
        ],
        tool_choice="auto",
    )

    assert payload["model"] == "gpt-5.1-codex"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["instructions"] == "System guidance"
    assert payload["input"][0]["role"] == "user"
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.9
    assert "max_output_tokens" not in payload
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["name"] == "lookup"


@pytest.mark.asyncio
async def test_non_stream_response_mapping_and_header_construction(
    provider: OpenAICodexProvider,
    credential_file: Path,
):
    endpoint = "https://chatgpt.com/backend-api/codex/responses"

    with respx.mock(assert_all_called=True) as mock_router:
        route = mock_router.post(endpoint)

        def responder(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("authorization", "").startswith("Bearer ")
            assert request.headers.get("chatgpt-account-id") == "acct_provider"
            assert request.headers.get("openai-beta") == "responses=experimental"
            assert request.headers.get("originator") == "pi"

            body = json.loads(request.content.decode("utf-8"))
            assert body["stream"] is True
            assert "instructions" in body
            assert "input" in body

            return httpx.Response(
                status_code=200,
                content=_build_sse_payload("pong"),
                headers={"content-type": "text/event-stream"},
            )

        route.mock(side_effect=responder)

        async with httpx.AsyncClient() as client:
            response = await provider.acompletion(
                client,
                model="openai_codex/gpt-5.1-codex",
                messages=[{"role": "user", "content": "say pong"}],
                stream=False,
                credential_identifier=str(credential_file),
            )

    assert response.choices[0]["message"]["content"] == "pong"
    assert response.usage["prompt_tokens"] == 5
    assert response.usage["completion_tokens"] == 3


@pytest.mark.asyncio
async def test_env_credential_identifier_supported(monkeypatch):
    provider = OpenAICodexProvider()

    payload = {
        "email": "env-provider@example.com",
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_env_provider"},
    }

    monkeypatch.setenv("OPENAI_CODEX_1_ACCESS_TOKEN", _build_jwt(payload))
    monkeypatch.setenv("OPENAI_CODEX_1_REFRESH_TOKEN", "rt_env_provider")

    endpoint = "https://chatgpt.com/backend-api/codex/responses"

    with respx.mock(assert_all_called=True) as mock_router:
        route = mock_router.post(endpoint)

        def responder(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("chatgpt-account-id") == "acct_env_provider"
            return httpx.Response(
                status_code=200,
                content=_build_sse_payload("env-ok"),
                headers={"content-type": "text/event-stream"},
            )

        route.mock(side_effect=responder)

        async with httpx.AsyncClient() as client:
            response = await provider.acompletion(
                client,
                model="openai_codex/gpt-5.1-codex",
                messages=[{"role": "user", "content": "test env"}],
                stream=False,
                credential_identifier="env://openai_codex/1",
            )

    assert response.choices[0]["message"]["content"] == "env-ok"


def test_parse_quota_error_from_retry_after_header(provider: OpenAICodexProvider):
    request = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    response = httpx.Response(
        status_code=429,
        request=request,
        headers={"Retry-After": "42"},
        text=json.dumps({"error": {"code": "rate_limit", "message": "Too many requests"}}),
    )
    error = httpx.HTTPStatusError("Rate limited", request=request, response=response)

    parsed = provider.parse_quota_error(error)
    assert parsed is not None
    assert parsed["retry_after"] == 42
    assert parsed["reason"] == "RATE_LIMIT"


def test_parse_quota_error_from_resets_at_field(provider: OpenAICodexProvider):
    now = int(time.time())
    reset_ts = now + 120

    request = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    response = httpx.Response(
        status_code=429,
        request=request,
        text=json.dumps(
            {
                "error": {
                    "code": "usage_limit",
                    "message": "quota exceeded",
                    "resets_at": reset_ts,
                }
            }
        ),
    )
    error = httpx.HTTPStatusError("Quota hit", request=request, response=response)

    parsed = provider.parse_quota_error(error)
    assert parsed is not None
    assert parsed["reason"] == "USAGE_LIMIT"
    assert parsed["quota_reset_timestamp"] == float(reset_ts)
    assert isinstance(parsed["retry_after"], int)
    assert parsed["retry_after"] >= 1


def test_parse_quota_error_does_not_match_generic_quota_substrings(
    provider: OpenAICodexProvider,
):
    request = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    response = httpx.Response(
        status_code=400,
        request=request,
        text=json.dumps(
            {
                "error": {
                    "code": "invalid_request_error",
                    "message": "quota project ID is invalid",
                }
            }
        ),
    )
    error = httpx.HTTPStatusError("Bad request", request=request, response=response)

    parsed = provider.parse_quota_error(error)
    assert parsed is None
