# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
OpenAI-compatible API routes.

This module contains all OpenAI-compatible endpoints including:
- Chat completions (/v1/chat/completions)
- Embeddings (/v1/embeddings)
- Models list (/v1/models)
- Token count (/v1/token-count)
- Cost estimate (/v1/cost-estimate)
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse, JSONResponse

import litellm
from rotator_library import RotatingClient

from proxy_app.dependencies import (
    get_rotating_client,
    get_embedding_batcher,
    get_model_info_service,
    verify_api_key,
)
from proxy_app.models import EmbeddingRequest
from proxy_app.streaming import streaming_response_wrapper
from proxy_app.detailed_logger import RawIOLogger
from proxy_app.request_logger import log_request_to_console
from proxy_app.error_mapping import map_litellm_error
from proxy_app.batch_manager import EmbeddingBatcher

logger = logging.getLogger(__name__)
router = APIRouter()

# Configuration from environment
ENABLE_RAW_LOGGING = os.getenv("ENABLE_RAW_LOGGING", "false").lower() == "true"
ENABLE_REQUEST_LOGGING = os.getenv("ENABLE_REQUEST_LOGGING", "false").lower() == "true"


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    client: RotatingClient = Depends(get_rotating_client),
    _=Depends(verify_api_key),
):
    """
    OpenAI-compatible chat completions endpoint.
    Handles both streaming and non-streaming responses.
    """
    raw_logger = RawIOLogger() if ENABLE_RAW_LOGGING else None

    try:
        # Read and parse the request body
        try:
            request_data = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in request body.")

        # Global temperature=0 override
        override_temp_zero = os.getenv("OVERRIDE_TEMPERATURE_ZERO", "false").lower()
        if (
            override_temp_zero in ("remove", "set", "true", "1", "yes")
            and "temperature" in request_data
            and request_data["temperature"] == 0
        ):
            if override_temp_zero == "remove":
                del request_data["temperature"]
                logger.debug("OVERRIDE_TEMPERATURE_ZERO=remove: Removed temperature=0")
            else:
                request_data["temperature"] = 1.0
                logger.debug("OVERRIDE_TEMPERATURE_ZERO=set: Changed temperature to 1.0")

        # Raw logging
        if raw_logger:
            raw_logger.log_request(headers=request.headers, body=request_data)

        # Log request
        log_request_to_console(
            url=str(request.url),
            headers=dict(request.headers),
            client_info=(request.client.host, request.client.port),
            request_data=request_data,
        )

        is_streaming = request_data.get("stream", False)

        if is_streaming:
            response_generator = await client.acompletion(
                request=request, **request_data
            )
            return StreamingResponse(
                streaming_response_wrapper(
                    request, request_data, response_generator, raw_logger
                ),
                media_type="text/event-stream",
            )
        else:
            response = await client.acompletion(request=request, **request_data)
            if raw_logger:
                response_headers = (
                    response.headers if hasattr(response, "headers") else None
                )
                status_code = (
                    response.status_code if hasattr(response, "status_code") else 200
                )
                raw_logger.log_final_response(
                    status_code=status_code,
                    headers=response_headers,
                    body=response.model_dump() if hasattr(response, "model_dump") else response,
                )
            return response

    except HTTPException:
        raise
    except Exception as e:
        raise map_litellm_error(e, "chat_completions")


@router.post("/v1/embeddings")
async def embeddings(
    request: Request,
    body: EmbeddingRequest,
    client: RotatingClient = Depends(get_rotating_client),
    batcher: Optional[EmbeddingBatcher] = Depends(get_embedding_batcher),
    _=Depends(verify_api_key),
):
    """
    OpenAI-compatible embeddings endpoint.
    Supports batched and direct pass-through modes.
    """
    try:
        request_data = body.model_dump(exclude_none=True)
        log_request_to_console(
            url=str(request.url),
            headers=dict(request.headers),
            client_info=(request.client.host, request.client.port),
            request_data=request_data,
        )

        USE_EMBEDDING_BATCHER = os.getenv("USE_EMBEDDING_BATCHER", "false").lower() == "true"

        if USE_EMBEDDING_BATCHER and batcher:
            # Server-side batching mode
            inputs = request_data.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]

            tasks = []
            for single_input in inputs:
                individual_request = request_data.copy()
                individual_request["input"] = single_input
                tasks.append(batcher.add_request(individual_request))

            results = await asyncio.gather(*tasks)

            all_data = []
            batch_usage = None
            for i, result in enumerate(results):
                result["data"][0]["index"] = i
                all_data.extend(result["data"])
                if i == 0 and result.get("usage"):
                    batch_usage = result["usage"]

            # Use batch usage or estimate
            if batch_usage:
                final_usage = batch_usage
            else:
                estimated_tokens = sum(len(str(inp)) // 4 for inp in inputs)
                final_usage = {
                    "prompt_tokens": estimated_tokens,
                    "total_tokens": estimated_tokens,
                }

            final_response_data = {
                "object": "list",
                "model": results[0]["model"],
                "data": all_data,
                "usage": final_usage,
            }
            response = litellm.EmbeddingResponse(**final_response_data)
        else:
            # Direct pass-through mode
            if isinstance(request_data.get("input"), str):
                request_data["input"] = [request_data["input"]]
            response = await client.aembedding(request=request, **request_data)

        return response

    except HTTPException:
        raise
    except Exception as e:
        raise map_litellm_error(e, "embeddings")


@router.get("/v1/models")
async def list_models(
    request: Request,
    client: RotatingClient = Depends(get_rotating_client),
    _=Depends(verify_api_key),
    enriched: bool = True,
):
    """Returns a list of available models in OpenAI-compatible format."""
    model_ids = await client.get_all_available_models(grouped=False)

    model_info_service = get_model_info_service(request)
    if enriched and model_info_service and model_info_service.is_ready:
        enriched_data = model_info_service.enrich_model_list(model_ids)
        return {"object": "list", "data": enriched_data}

    # Fallback to basic model cards
    model_cards = [
        {
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "Mirro-Proxy",
        }
        for model_id in model_ids
    ]
    return {"object": "list", "data": model_cards}


@router.get("/v1/models/{model_id:path}")
async def get_model(
    model_id: str,
    request: Request,
    _=Depends(verify_api_key),
):
    """Returns detailed information about a specific model."""
    model_info_service = get_model_info_service(request)
    if model_info_service and model_info_service.is_ready:
        info = model_info_service.get_model_info(model_id)
        if info:
            return info.to_dict()

    # Return basic info
    return {
        "id": model_id,
        "object": "model",
        "created": int(time.time()),
        "owned_by": model_id.split("/")[0] if "/" in model_id else "unknown",
    }


@router.post("/v1/token-count")
async def token_count(
    request: Request,
    client: RotatingClient = Depends(get_rotating_client),
    _=Depends(verify_api_key),
):
    """Calculates the token count for a given list of messages and a model."""
    try:
        data = await request.json()
        model = data.get("model")
        messages = data.get("messages")

        if not model or not messages:
            raise HTTPException(
                status_code=400, detail="'model' and 'messages' are required."
            )

        count = client.token_count(**data)
        return {"token_count": count}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token count failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/v1/cost-estimate")
async def cost_estimate(request: Request, _=Depends(verify_api_key)):
    """
    Estimates the cost for a request based on token counts and model pricing.
    """
    try:
        data = await request.json()
        model = data.get("model")
        prompt_tokens = data.get("prompt_tokens", 0)
        completion_tokens = data.get("completion_tokens", 0)
        cache_read_tokens = data.get("cache_read_tokens", 0)
        cache_creation_tokens = data.get("cache_creation_tokens", 0)

        if not model:
            raise HTTPException(status_code=400, detail="'model' is required.")

        result = {
            "model": model,
            "cost": None,
            "currency": "USD",
            "pricing": {},
            "source": None,
        }

        # Try model info service first
        model_info_service = get_model_info_service(request)
        if model_info_service and model_info_service.is_ready:
            cost = model_info_service.calculate_cost(
                model,
                prompt_tokens,
                completion_tokens,
                cache_read_tokens,
                cache_creation_tokens,
            )
            if cost is not None:
                cost_info = model_info_service.get_cost_info(model)
                result["cost"] = cost
                result["pricing"] = cost_info or {}
                result["source"] = "model_info_service"
                return result

        # Fallback to litellm
        try:
            model_info = litellm.get_model_info(model)
            input_cost = model_info.get("input_cost_per_token", 0)
            output_cost = model_info.get("output_cost_per_token", 0)

            if input_cost or output_cost:
                cost = (prompt_tokens * input_cost) + (completion_tokens * output_cost)
                result["cost"] = cost
                result["pricing"] = {
                    "input_cost_per_token": input_cost,
                    "output_cost_per_token": output_cost,
                }
                result["source"] = "litellm_fallback"
                return result
        except Exception:
            pass

        result["source"] = "unknown"
        result["error"] = "Pricing data not available for this model"
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cost estimate failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/")
def read_root():
    """Root endpoint returning proxy status."""
    return {"Status": "API Key Proxy is running"}
