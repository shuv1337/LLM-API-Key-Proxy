# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
Anthropic-compatible API routes.

This module contains all Anthropic-compatible endpoints including:
- Messages (/v1/messages)
- Token count (/v1/messages/count_tokens)
"""

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse

from rotator_library import RotatingClient
from rotator_library.anthropic_compat import (
    AnthropicMessagesRequest,
    AnthropicCountTokensRequest,
)

from proxy_app.dependencies import (
    get_rotating_client,
    verify_anthropic_api_key,
)
from proxy_app.detailed_logger import RawIOLogger
from proxy_app.request_logger import log_request_to_console
from proxy_app.error_mapping import map_litellm_error_to_anthropic

import os

logger = logging.getLogger(__name__)
router = APIRouter()

ENABLE_RAW_LOGGING = os.getenv("ENABLE_RAW_LOGGING", "false").lower() == "true"


@router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    body: AnthropicMessagesRequest,
    client: RotatingClient = Depends(get_rotating_client),
    _=Depends(verify_anthropic_api_key),
):
    """
    Anthropic-compatible Messages API endpoint.

    Accepts requests in Anthropic's format and returns responses in Anthropic's format.
    Internally translates to OpenAI format for processing via LiteLLM.
    """
    raw_logger = RawIOLogger() if ENABLE_RAW_LOGGING else None

    # Log raw Anthropic request if raw logging is enabled
    if raw_logger:
        raw_logger.log_request(
            headers=dict(request.headers),
            body=body.model_dump(exclude_none=True),
        )

    try:
        # Log the request to console
        log_request_to_console(
            url=str(request.url),
            headers=dict(request.headers),
            client_info=(
                request.client.host if request.client else "unknown",
                request.client.port if request.client else 0,
            ),
            request_data=body.model_dump(exclude_none=True),
        )

        # Use the library method to handle the request
        result = await client.anthropic_messages(body, raw_request=request)

        if body.stream:
            # Streaming response
            return StreamingResponse(
                result,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # Non-streaming response
            if raw_logger:
                raw_logger.log_final_response(
                    status_code=200,
                    headers=None,
                    body=result,
                )
            return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Anthropic messages endpoint error: {e}")
        if raw_logger:
            raw_logger.log_final_response(
                status_code=500,
                headers=None,
                body={"error": str(e)},
            )
        raise map_litellm_error_to_anthropic(e, "anthropic_messages")


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    body: AnthropicCountTokensRequest,
    client: RotatingClient = Depends(get_rotating_client),
    _=Depends(verify_anthropic_api_key),
):
    """
    Anthropic-compatible count_tokens endpoint.

    Counts the number of tokens that would be used by a Messages API request.
    """
    try:
        # Use the library method to handle the request
        result = await client.anthropic_count_tokens(body)
        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Anthropic count_tokens endpoint error: {e}")
        raise map_litellm_error_to_anthropic(e, "anthropic_count_tokens")
