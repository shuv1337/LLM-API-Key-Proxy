# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 Mirrowel

# src/rotator_library/providers/openai_codex_provider.py

import copy
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Union

import httpx
import litellm

from .openai_codex_auth_base import (
    AUTH_CLAIM,
    DEFAULT_API_BASE,
    RESPONSES_ENDPOINT_PATH,
    OpenAICodexAuthBase,
)
from .provider_interface import ProviderInterface, UsageResetConfigDef, QuotaGroupMap
from ..model_definitions import ModelDefinitions
from ..timeout_config import TimeoutConfig
from ..transaction_logger import ProviderLogger

lib_logger = logging.getLogger("rotator_library")

# Conservative fallback model list (can be overridden via OPENAI_CODEX_MODELS)
HARDCODED_MODELS = [
    "gpt-5.1-codex",
    "gpt-5-codex",
    "gpt-4.1-codex",
]


class CodexStreamError(Exception):
    """Terminal Codex stream error that should abort the stream."""

    def __init__(self, message: str, status_code: int = 500, error_body: Optional[str] = None):
        self.status_code = status_code
        self.error_body = error_body or message
        super().__init__(message)


class CodexSSETranslator:
    """
    Translates OpenAI Codex SSE events into OpenAI chat.completion chunks.

    Supports both currently observed events and planned fallback aliases:
    - response.output_text.delta (observed)
    - response.content_part.delta (planned alias)
    - response.function_call_arguments.delta / .done
    """

    def __init__(self, model_id: str):
        self.model_id = model_id
        self.response_id: Optional[str] = None
        self.created: int = int(time.time())
        self._tool_index_by_call_id: Dict[str, int] = {}
        self._tool_names_by_call_id: Dict[str, str] = {}

    def _build_chunk(
        self,
        *,
        delta: Optional[Dict[str, Any]] = None,
        finish_reason: Optional[str] = None,
        usage: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        if not self.response_id:
            self.response_id = f"chatcmpl-codex-{int(time.time() * 1000)}"

        choice = {
            "index": 0,
            "delta": delta or {},
            "finish_reason": finish_reason,
        }

        chunk = {
            "id": self.response_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model_id,
            "choices": [choice],
        }

        if usage is not None:
            chunk["usage"] = usage

        return chunk

    def _extract_text_delta(self, event: Dict[str, Any]) -> Optional[str]:
        event_type = event.get("type")

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                return delta

        if event_type == "response.content_part.delta":
            # Compatibility with planned taxonomy
            if isinstance(event.get("delta"), str):
                return event["delta"]
            part = event.get("part")
            if isinstance(part, dict):
                if isinstance(part.get("delta"), str):
                    return part["delta"]
                if isinstance(part.get("text"), str):
                    return part["text"]

        if event_type == "response.content_part.added":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    return text

        return None

    def _map_incomplete_reason(self, reason: Optional[str]) -> str:
        if not reason:
            return "length"

        normalized = reason.strip().lower()
        if normalized in {"stop", "completed"}:
            return "stop"
        if normalized in {"max_output_tokens", "max_tokens", "length"}:
            return "length"
        if normalized in {"tool_calls", "tool_call"}:
            return "tool_calls"
        if normalized in {"content_filter", "content_filtered"}:
            return "content_filter"
        return "length"

    def _extract_usage(self, event: Dict[str, Any]) -> Optional[Dict[str, int]]:
        response = event.get("response")
        if not isinstance(response, dict):
            return None

        usage = response.get("usage")
        if not isinstance(usage, dict):
            return None

        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        completion_tokens = int(usage.get("output_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0)

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _get_response_status(self, event: Dict[str, Any]) -> str:
        response = event.get("response")
        if isinstance(response, dict):
            status = response.get("status")
            if isinstance(status, str) and status:
                return status

        event_type = event.get("type")
        if event_type == "response.incomplete":
            return "incomplete"
        if event_type == "response.failed":
            return "failed"
        return "completed"

    def _get_or_create_tool_index(self, call_id: str) -> int:
        if call_id not in self._tool_index_by_call_id:
            self._tool_index_by_call_id[call_id] = len(self._tool_index_by_call_id)
        return self._tool_index_by_call_id[call_id]

    def _extract_tool_call_id(self, event: Dict[str, Any]) -> Optional[str]:
        for key in ("call_id", "item_id", "id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value

        item = event.get("item")
        if isinstance(item, dict):
            for key in ("call_id", "id"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    return value

        return None

    def _extract_error_payload(self, event: Dict[str, Any]) -> Dict[str, Any]:
        # Common formats:
        # {type:"error", error:{...}}
        # {type:"response.failed", response:{error:{...}}}
        payload = event.get("error")
        if isinstance(payload, dict):
            return payload

        response = event.get("response")
        if isinstance(response, dict):
            nested = response.get("error")
            if isinstance(nested, dict):
                return nested

        return {}

    def _classify_error_status(self, error_payload: Dict[str, Any]) -> int:
        code = str(error_payload.get("code", "") or "").lower()
        err_type = str(error_payload.get("type", "") or "").lower()
        message = str(error_payload.get("message", "") or "").lower()
        text = " ".join([code, err_type, message])

        if any(token in text for token in ["rate_limit", "usage_limit", "quota"]):
            return 429
        if any(token in text for token in ["auth", "unauthorized", "invalid_api_key"]):
            return 401
        if "forbidden" in text:
            return 403
        if "context" in text or "max_output_tokens" in text:
            return 400
        return 500

    def process_event(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Process a single SSE event and return zero or more translated chunks."""
        chunks: List[Dict[str, Any]] = []

        event_type = event.get("type")
        if not isinstance(event_type, str):
            return chunks

        # Capture response id/created as early as possible
        response = event.get("response")
        if isinstance(response, dict):
            if isinstance(response.get("id"), str) and response.get("id"):
                self.response_id = response["id"]
            if isinstance(response.get("created_at"), (int, float)):
                self.created = int(response["created_at"])

        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                call_id = self._extract_tool_call_id(item)
                if call_id:
                    index = self._get_or_create_tool_index(call_id)
                    name = item.get("name") if isinstance(item.get("name"), str) else ""
                    if name:
                        self._tool_names_by_call_id[call_id] = name

                    initial_args = item.get("arguments")
                    if not isinstance(initial_args, str):
                        initial_args = ""

                    tool_delta = {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": initial_args,
                                },
                            }
                        ]
                    }
                    chunks.append(self._build_chunk(delta=tool_delta))
            return chunks

        if event_type == "response.function_call_arguments.delta":
            call_id = self._extract_tool_call_id(event)
            delta = event.get("delta")
            if call_id and isinstance(delta, str):
                index = self._get_or_create_tool_index(call_id)
                name = self._tool_names_by_call_id.get(call_id, "")
                tool_delta = {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": delta,
                            },
                        }
                    ]
                }
                chunks.append(self._build_chunk(delta=tool_delta))
            return chunks

        if event_type == "response.function_call_arguments.done":
            call_id = self._extract_tool_call_id(event)
            if call_id:
                index = self._get_or_create_tool_index(call_id)
                name = self._tool_names_by_call_id.get(call_id, "")
                arguments = event.get("arguments")
                if not isinstance(arguments, str):
                    arguments = ""

                tool_delta = {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments,
                            },
                        }
                    ]
                }
                chunks.append(self._build_chunk(delta=tool_delta))
            return chunks

        text_delta = self._extract_text_delta(event)
        if text_delta:
            chunks.append(self._build_chunk(delta={"content": text_delta}))
            return chunks

        if event_type in ("error", "response.failed"):
            error_payload = self._extract_error_payload(event)
            status_code = self._classify_error_status(error_payload)
            message = (
                error_payload.get("message")
                if isinstance(error_payload.get("message"), str)
                else f"Codex stream failed ({event_type})"
            )
            raise CodexStreamError(
                message=message,
                status_code=status_code,
                error_body=json.dumps({"error": error_payload} if error_payload else event),
            )

        if event_type in ("response.completed", "response.incomplete"):
            usage = self._extract_usage(event)
            status = self._get_response_status(event)
            finish_reason = "stop"

            if status == "incomplete":
                incomplete_details = None
                if isinstance(response, dict):
                    incomplete_details = response.get("incomplete_details")
                reason = None
                if isinstance(incomplete_details, dict):
                    reason = incomplete_details.get("reason")
                if isinstance(reason, str):
                    finish_reason = self._map_incomplete_reason(reason)
                else:
                    finish_reason = "length"

            chunks.append(
                self._build_chunk(delta={}, finish_reason=finish_reason, usage=usage)
            )
            return chunks

        # Ignore all other event families safely
        return chunks


class OpenAICodexProvider(OpenAICodexAuthBase, ProviderInterface):
    """OpenAI Codex provider via ChatGPT backend `/codex/responses`."""

    skip_cost_calculation = True
    default_rotation_mode: str = "sequential"
    provider_env_name: str = "openai_codex"

    # Conservative placeholders (MVP-safe defaults)
    tier_priorities = {
        "unknown": 10,
    }

    usage_reset_configs = {
        "default": UsageResetConfigDef(
            window_seconds=24 * 60 * 60,
            mode="credential",
            description="TODO: tune OpenAI Codex quota window from observed behavior",
            field_name="daily",
        )
    }

    model_quota_groups: QuotaGroupMap = {
        # TODO: tune once quota sharing behavior is empirically validated
    }

    def __init__(self):
        super().__init__()
        self.model_definitions = ModelDefinitions()

    def has_custom_logic(self) -> bool:
        return True

    # =========================================================================
    # Model discovery
    # =========================================================================

    async def get_models(self, credential: str, client: httpx.AsyncClient) -> List[str]:
        """
        Returns OpenAI Codex models from:
        1) OPENAI_CODEX_MODELS env definitions (priority)
        2) hardcoded fallback list
        3) optional dynamic /models discovery (best-effort)
        """
        models: List[str] = []
        env_model_ids = set()

        static_models = self.model_definitions.get_all_provider_models("openai_codex")
        if static_models:
            for model in static_models:
                model_name = model.split("/")[-1] if "/" in model else model
                model_id = self.model_definitions.get_model_id("openai_codex", model_name)
                models.append(model)
                if model_id:
                    env_model_ids.add(model_id)

            lib_logger.info(
                f"Loaded {len(static_models)} static models for openai_codex from OPENAI_CODEX_MODELS"
            )

        for model_id in HARDCODED_MODELS:
            if model_id not in env_model_ids:
                models.append(f"openai_codex/{model_id}")
                env_model_ids.add(model_id)

        # Optional dynamic discovery (Codex backend may not support this endpoint)
        try:
            await self.initialize_token(credential)
            creds = await self._load_credentials(credential)
            access_token, account_id = self._extract_runtime_auth(creds)

            api_base = self._resolve_api_base()
            models_url = f"{api_base.rstrip('/')}/models"

            headers = self._build_request_headers(
                access_token=access_token,
                account_id=account_id,
                stream=False,
            )

            response = await client.get(models_url, headers=headers, timeout=20.0)
            response.raise_for_status()

            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else payload

            discovered = 0
            if isinstance(data, list):
                for item in data:
                    model_id = None
                    if isinstance(item, dict):
                        model_id = item.get("id") or item.get("name")
                    elif isinstance(item, str):
                        model_id = item

                    if isinstance(model_id, str) and model_id and model_id not in env_model_ids:
                        models.append(f"openai_codex/{model_id}")
                        env_model_ids.add(model_id)
                        discovered += 1

            if discovered > 0:
                lib_logger.debug(
                    f"Discovered {discovered} additional models for openai_codex via dynamic /models"
                )

        except Exception as e:
            lib_logger.debug(f"Dynamic model discovery failed for openai_codex: {e}")

        return models

    async def initialize_credentials(self, credential_paths: List[str]) -> None:
        """Preload credentials and queue refresh/reauth where needed."""
        ready = 0
        refreshing = 0
        reauth_required = 0

        for cred_path in credential_paths:
            try:
                creds = await self._load_credentials(cred_path)
                self._ensure_proxy_metadata(creds)

                if not creds.get("refresh_token"):
                    await self._queue_refresh(cred_path, force=True, needs_reauth=True)
                    reauth_required += 1
                    continue

                if self._is_token_expired(creds):
                    await self._queue_refresh(cred_path, force=False, needs_reauth=False)
                    refreshing += 1
                else:
                    ready += 1

                # ensure metadata caches are populated
                self._credentials_cache[cred_path] = creds

            except Exception as e:
                lib_logger.warning(
                    f"Failed to initialize OpenAI Codex credential '{cred_path}': {e}"
                )
                await self._queue_refresh(cred_path, force=True, needs_reauth=True)
                reauth_required += 1

        lib_logger.info(
            "OpenAI Codex credential initialization: "
            f"ready={ready}, refreshing={refreshing}, reauth_required={reauth_required}"
        )

    # =========================================================================
    # Request mapping helpers
    # =========================================================================

    def _resolve_api_base(self) -> str:
        return os.getenv("OPENAI_CODEX_API_BASE", DEFAULT_API_BASE)

    def _extract_runtime_auth(self, creds: Dict[str, Any]) -> Tuple[str, str]:
        access_token = creds.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("OpenAI Codex credential missing access_token")

        metadata = creds.get("_proxy_metadata", {})
        account_id = metadata.get("account_id")

        if not account_id:
            # Fallback parse from access_token
            payload = self._decode_jwt_unverified(access_token)
            if payload:
                direct = payload.get("https://api.openai.com/auth.chatgpt_account_id")
                nested = None
                claim = payload.get(AUTH_CLAIM)
                if isinstance(claim, dict):
                    nested = claim.get("chatgpt_account_id")

                account_id = direct or nested

        if not isinstance(account_id, str) or not account_id:
            raise ValueError(
                "OpenAI Codex credential missing account_id. Re-authenticate to refresh token metadata."
            )

        return access_token, account_id

    def _build_request_headers(
        self,
        *,
        access_token: str,
        account_id: str,
        stream: bool,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "pi",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "LLM-API-Key-Proxy/OpenAICodex",
        }

        if extra_headers:
            headers.update({k: str(v) for k, v in extra_headers.items()})

        return headers

    def _extract_text(self, content: Any) -> str:
        if content is None:
            return ""

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    # OpenAI chat content blocks
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif item.get("type") in {"input_text", "output_text"} and isinstance(
                        item.get("text"), str
                    ):
                        parts.append(item["text"])
                    elif item.get("type") == "refusal" and isinstance(item.get("refusal"), str):
                        parts.append(item["refusal"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)

        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return content["text"]
            return json.dumps(content)

        return str(content)

    def _convert_user_content_to_input_parts(self, content: Any) -> List[Dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "input_text", "text": content}]

        if isinstance(content, list):
            parts: List[Dict[str, Any]] = []
            for item in content:
                if not isinstance(item, dict):
                    continue

                item_type = item.get("type")
                if item_type in ("text", "input_text") and isinstance(item.get("text"), str):
                    parts.append({"type": "input_text", "text": item["text"]})
                elif item_type == "image_url":
                    image_url = item.get("image_url")
                    if isinstance(image_url, dict):
                        image_url = image_url.get("url")
                    if isinstance(image_url, str) and image_url:
                        parts.append({"type": "input_image", "image_url": image_url, "detail": "auto"})
                elif item_type == "input_image":
                    image_url = item.get("image_url")
                    if isinstance(image_url, str) and image_url:
                        part = {"type": "input_image", "image_url": image_url}
                        if isinstance(item.get("detail"), str):
                            part["detail"] = item["detail"]
                        else:
                            part["detail"] = "auto"
                        parts.append(part)

            if parts:
                return parts

        text = self._extract_text(content)
        return [{"type": "input_text", "text": text}]

    def _convert_messages_to_codex_input(
        self,
        messages: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        instructions: List[str] = []
        codex_input: List[Dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if role in ("system", "developer"):
                text = self._extract_text(content)
                if text.strip():
                    instructions.append(text.strip())
                continue

            if role == "user":
                codex_input.append(
                    {
                        "role": "user",
                        "content": self._convert_user_content_to_input_parts(content),
                    }
                )
                continue

            if role == "assistant":
                text = self._extract_text(content)
                if text.strip():
                    codex_input.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )

                # Carry forward assistant tool calls where provided
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        if not isinstance(tool_call, dict):
                            continue

                        call_id = tool_call.get("id")
                        function = tool_call.get("function", {})
                        if not isinstance(function, dict):
                            continue

                        name = function.get("name")
                        arguments = function.get("arguments")
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments or {})

                        if isinstance(call_id, str) and isinstance(name, str):
                            codex_input.append(
                                {
                                    "type": "function_call",
                                    "call_id": call_id,
                                    "name": name,
                                    "arguments": arguments,
                                }
                            )
                continue

            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id:
                    continue

                output_text = self._extract_text(content)
                codex_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output_text,
                    }
                )

        # Codex endpoint currently requires non-empty instructions
        instructions_text = "\n\n".join(instructions).strip()
        if not instructions_text:
            instructions_text = "You are a helpful assistant."

        if not codex_input:
            codex_input = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "",
                        }
                    ],
                }
            ]

        return instructions_text, codex_input

    def _convert_tools(self, tools: Any) -> Optional[List[Dict[str, Any]]]:
        if not isinstance(tools, list) or not tools:
            return None

        converted: List[Dict[str, Any]] = []

        for tool in tools:
            if not isinstance(tool, dict):
                continue

            # OpenAI chat format: {type:"function", function:{name,description,parameters}}
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                name = fn.get("name")
                if not isinstance(name, str) or not name:
                    continue

                schema = fn.get("parameters")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}

                # Remove OpenAI-specific strict flag if present
                schema = copy.deepcopy(schema)
                schema.pop("additionalProperties", None)

                converted.append(
                    {
                        "type": "function",
                        "name": name,
                        "description": fn.get("description", ""),
                        "parameters": schema,
                    }
                )
                continue

            # Already in responses format
            if tool.get("type") == "function" and isinstance(tool.get("name"), str):
                converted.append(copy.deepcopy(tool))

        return converted or None

    def _normalize_tool_choice(self, tool_choice: Any, has_tools: bool) -> Any:
        if not has_tools:
            return None

        if isinstance(tool_choice, str):
            # Codex endpoint handles "auto" reliably; map required -> auto
            if tool_choice in {"auto", "none"}:
                return tool_choice
            if tool_choice == "required":
                return "auto"
            return "auto"

        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "function":
                fn = tool_choice.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    return {"type": "function", "name": fn["name"]}
                if isinstance(tool_choice.get("name"), str):
                    return {"type": "function", "name": tool_choice["name"]}
            if isinstance(tool_choice.get("name"), str):
                return {"type": "function", "name": tool_choice["name"]}

        return "auto"

    def _build_codex_payload(self, model_name: str, **kwargs) -> Dict[str, Any]:
        messages = kwargs.get("messages") or []
        instructions, codex_input = self._convert_messages_to_codex_input(messages)

        payload: Dict[str, Any] = {
            "model": model_name,
            "stream": True,  # Endpoint currently requires stream=true
            "store": False,
            "instructions": instructions,
            "input": codex_input,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }

        # Keep verbosity at medium by default (gpt-5.1-codex rejects low)
        text_verbosity = os.getenv("OPENAI_CODEX_TEXT_VERBOSITY", "medium")
        payload["text"] = {"verbosity": text_verbosity}

        # OpenAI chat params -> Codex responses equivalents
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs["temperature"]
        if kwargs.get("top_p") is not None:
            payload["top_p"] = kwargs["top_p"]
        if kwargs.get("max_tokens") is not None:
            payload["max_output_tokens"] = kwargs["max_tokens"]

        converted_tools = self._convert_tools(kwargs.get("tools"))
        if converted_tools:
            payload["tools"] = converted_tools
            payload["tool_choice"] = self._normalize_tool_choice(
                kwargs.get("tool_choice"),
                has_tools=True,
            )
            payload["parallel_tool_calls"] = True
        else:
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            payload.pop("parallel_tool_calls", None)

        # Optional session pinning for cache affinity
        session_id = kwargs.get("session_id") or kwargs.get("conversation_id")
        if isinstance(session_id, str) and session_id:
            payload["prompt_cache_key"] = session_id
            payload["prompt_cache_retention"] = "in-memory"

        return payload

    # =========================================================================
    # SSE parsing + response conversion
    # =========================================================================

    async def _iter_sse_events(
        self, response: httpx.Response
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Parse SSE stream into event dictionaries."""
        event_lines: List[str] = []

        async for line in response.aiter_lines():
            if line is None:
                continue

            if line == "":
                if not event_lines:
                    continue

                data_lines = []
                for entry in event_lines:
                    if entry.startswith("data:"):
                        data_lines.append(entry[5:].lstrip())

                event_lines = []
                if not data_lines:
                    continue

                payload = "\n".join(data_lines).strip()
                if not payload or payload == "[DONE]":
                    if payload == "[DONE]":
                        return
                    continue

                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, dict):
                        yield parsed
                except json.JSONDecodeError:
                    lib_logger.debug(f"OpenAI Codex SSE non-JSON payload ignored: {payload[:200]}")
                continue

            event_lines.append(line)

        # Flush trailing event if stream closes without blank line
        if event_lines:
            data_lines = [entry[5:].lstrip() for entry in event_lines if entry.startswith("data:")]
            payload = "\n".join(data_lines).strip()
            if payload and payload != "[DONE]":
                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, dict):
                        yield parsed
                except json.JSONDecodeError:
                    pass

    def _stream_to_completion_response(
        self, chunks: List[litellm.ModelResponse]
    ) -> litellm.ModelResponse:
        """Reassemble streamed chunks into a non-streaming ModelResponse."""
        if not chunks:
            raise ValueError("No chunks provided for reassembly")

        final_message: Dict[str, Any] = {"role": "assistant"}
        aggregated_tool_calls: Dict[int, Dict[str, Any]] = {}
        usage_data = None
        chunk_finish_reason = None

        first_chunk = chunks[0]

        for chunk in chunks:
            if not hasattr(chunk, "choices") or not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.get("delta", {})

            if "content" in delta and delta["content"] is not None:
                final_message["content"] = final_message.get("content", "") + delta["content"]

            if "tool_calls" in delta and delta["tool_calls"]:
                for tc_chunk in delta["tool_calls"]:
                    index = tc_chunk.get("index", 0)
                    if index not in aggregated_tool_calls:
                        aggregated_tool_calls[index] = {
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }

                    if tc_chunk.get("id"):
                        aggregated_tool_calls[index]["id"] = tc_chunk["id"]

                    if tc_chunk.get("type"):
                        aggregated_tool_calls[index]["type"] = tc_chunk["type"]

                    if isinstance(tc_chunk.get("function"), dict):
                        fn = tc_chunk["function"]
                        if fn.get("name") is not None:
                            aggregated_tool_calls[index]["function"]["name"] += str(fn["name"])
                        if fn.get("arguments") is not None:
                            aggregated_tool_calls[index]["function"]["arguments"] += str(
                                fn["arguments"]
                            )

            if choice.get("finish_reason"):
                chunk_finish_reason = choice["finish_reason"]

        for chunk in reversed(chunks):
            if hasattr(chunk, "usage") and chunk.usage:
                usage_data = chunk.usage
                break

        if aggregated_tool_calls:
            final_message["tool_calls"] = list(aggregated_tool_calls.values())

        for field in ["content", "tool_calls", "function_call"]:
            if field not in final_message:
                final_message[field] = None

        if aggregated_tool_calls:
            finish_reason = "tool_calls"
        elif chunk_finish_reason:
            finish_reason = chunk_finish_reason
        else:
            finish_reason = "stop"

        final_choice = {
            "index": 0,
            "message": final_message,
            "finish_reason": finish_reason,
        }

        final_response_data = {
            "id": first_chunk.id,
            "object": "chat.completion",
            "created": first_chunk.created,
            "model": first_chunk.model,
            "choices": [final_choice],
            "usage": usage_data,
        }

        return litellm.ModelResponse(**final_response_data)

    # =========================================================================
    # Main completion flow
    # =========================================================================

    async def acompletion(
        self, client: httpx.AsyncClient, **kwargs
    ) -> Union[litellm.ModelResponse, AsyncGenerator[litellm.ModelResponse, None]]:
        credential_identifier = kwargs.pop("credential_identifier")
        transaction_context = kwargs.pop("transaction_context", None)
        model = kwargs["model"]

        file_logger = ProviderLogger(transaction_context)

        async def make_request() -> Any:
            # Ensure token initialized/refreshed before request
            await self.initialize_token(credential_identifier)
            creds = await self._load_credentials(credential_identifier)
            if self._is_token_expired(creds):
                creds = await self._refresh_token(credential_identifier)

            access_token, account_id = self._extract_runtime_auth(creds)

            model_name = model.split("/")[-1]
            payload = self._build_codex_payload(model_name=model_name, **kwargs)

            headers = self._build_request_headers(
                access_token=access_token,
                account_id=account_id,
                stream=True,
            )

            url = f"{self._resolve_api_base().rstrip('/')}{RESPONSES_ENDPOINT_PATH}"
            file_logger.log_request(payload)

            return client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=TimeoutConfig.streaming(),
            )

        async def stream_handler(
            response_stream: Any,
            attempt: int = 1,
        ):
            try:
                async with response_stream as response:
                    if response.status_code >= 400:
                        raw_error = await response.aread()
                        error_text = (
                            raw_error.decode("utf-8", "replace")
                            if isinstance(raw_error, bytes)
                            else str(raw_error)
                        )

                        # Try a single forced token refresh on auth failures
                        if response.status_code in (401, 403) and attempt == 1:
                            lib_logger.warning(
                                "OpenAI Codex returned 401/403; forcing refresh and retrying once"
                            )
                            await self._refresh_token(credential_identifier, force=True)
                            retry_stream = await make_request()
                            async for chunk in stream_handler(retry_stream, attempt=2):
                                yield chunk
                            return

                        # Surface typed HTTPStatusError for classify_error()
                        raise httpx.HTTPStatusError(
                            f"OpenAI Codex HTTP {response.status_code}: {error_text}",
                            request=response.request,
                            response=response,
                        )

                    translator = CodexSSETranslator(model_id=model)

                    async for event in self._iter_sse_events(response):
                        try:
                            file_logger.log_response_chunk(json.dumps(event))
                        except Exception:
                            pass

                        try:
                            translated_chunks = translator.process_event(event)
                        except CodexStreamError as stream_error:
                            synthetic_response = httpx.Response(
                                status_code=stream_error.status_code,
                                request=response.request,
                                text=stream_error.error_body,
                            )
                            raise httpx.HTTPStatusError(
                                str(stream_error),
                                request=response.request,
                                response=synthetic_response,
                            )

                        for chunk_dict in translated_chunks:
                            yield litellm.ModelResponse(**chunk_dict)

            except httpx.HTTPStatusError:
                raise
            except Exception as e:
                file_logger.log_error(f"Error during OpenAI Codex stream processing: {e}")
                raise

        async def logging_stream_wrapper():
            chunks: List[litellm.ModelResponse] = []
            try:
                async for chunk in stream_handler(await make_request()):
                    chunks.append(chunk)
                    yield chunk
            finally:
                if chunks:
                    try:
                        final_response = self._stream_to_completion_response(chunks)
                        if hasattr(final_response, "model_dump"):
                            file_logger.log_final_response(final_response.model_dump())
                        else:
                            file_logger.log_final_response(final_response.dict())
                    except Exception:
                        pass

        if kwargs.get("stream"):
            return logging_stream_wrapper()

        async def non_stream_wrapper() -> litellm.ModelResponse:
            chunks = [chunk async for chunk in logging_stream_wrapper()]
            return self._stream_to_completion_response(chunks)

        return await non_stream_wrapper()

    # =========================================================================
    # Provider-specific quota parsing
    # =========================================================================

    @staticmethod
    def parse_quota_error(
        error: Exception,
        error_body: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Parse OpenAI Codex quota/rate-limit errors.

        Supports:
        - Retry-After header
        - error.resets_at (unix seconds)
        - error.retry_after / retry_after_seconds fields
        - usage_limit / quota / rate_limit style error codes
        """
        now_ts = time.time()

        response = None
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response

        headers = response.headers if response is not None else {}

        retry_after: Optional[int] = None
        retry_header = headers.get("Retry-After") or headers.get("retry-after")
        if retry_header:
            try:
                retry_after = max(1, int(float(retry_header)))
            except ValueError:
                retry_after = None

        body_text = error_body
        if body_text is None and response is not None:
            try:
                body_text = response.text
            except Exception:
                body_text = None

        if not body_text:
            if retry_after is not None:
                return {
                    "retry_after": retry_after,
                    "reason": "RATE_LIMIT",
                    "reset_timestamp": None,
                    "quota_reset_timestamp": None,
                }
            return None

        parsed = None
        try:
            parsed = json.loads(body_text)
        except Exception:
            parsed = None

        if not isinstance(parsed, dict):
            if retry_after is not None:
                return {
                    "retry_after": retry_after,
                    "reason": "RATE_LIMIT",
                    "reset_timestamp": None,
                    "quota_reset_timestamp": None,
                }
            return None

        err = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}

        code = str(err.get("code", "") or "").lower()
        err_type = str(err.get("type", "") or "").lower()
        message = str(err.get("message", "") or "").lower()
        combined = " ".join([code, err_type, message])

        # Look for codex-specific reset timestamp
        reset_ts = err.get("resets_at")
        quota_reset_timestamp: Optional[float] = None
        reset_timestamp_iso: Optional[str] = None
        if isinstance(reset_ts, (int, float)):
            quota_reset_timestamp = float(reset_ts)
            retry_after_from_reset = int(max(1, quota_reset_timestamp - now_ts))
            retry_after = retry_after or retry_after_from_reset
            reset_timestamp_iso = datetime.fromtimestamp(
                quota_reset_timestamp, tz=timezone.utc
            ).isoformat()

        if retry_after is None:
            for key in ("retry_after", "retry_after_seconds", "retryAfter"):
                value = err.get(key)
                if isinstance(value, (int, float)):
                    retry_after = max(1, int(value))
                    break
                if isinstance(value, str):
                    try:
                        retry_after = max(1, int(float(value)))
                        break
                    except ValueError:
                        continue

        if retry_after is None and any(
            token in combined for token in ["usage_limit", "rate_limit", "quota"]
        ):
            retry_after = 60

        if retry_after is None:
            return None

        reason = (
            str(err.get("code") or err.get("type") or "RATE_LIMIT").upper()
        )

        return {
            "retry_after": retry_after,
            "reason": reason,
            "reset_timestamp": reset_timestamp_iso,
            "quota_reset_timestamp": quota_reset_timestamp,
        }
