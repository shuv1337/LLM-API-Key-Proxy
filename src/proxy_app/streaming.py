# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
Streaming response handling for the proxy application.

This module provides the streaming_response_wrapper function and related utilities
for handling SSE streams from LiteLLM.
"""

import json
import logging
from typing import AsyncGenerator, Optional, Any, Dict

from fastapi import Request

from proxy_app.detailed_logger import RawIOLogger

logger = logging.getLogger(__name__)


async def streaming_response_wrapper(
    request: Request,
    request_data: dict,
    response_stream: AsyncGenerator[str, None],
    logger_instance: Optional[RawIOLogger] = None,
) -> AsyncGenerator[str, None]:
    """
    Wraps a streaming response to log the full response after completion
    and ensures any errors during the stream are sent to the client.

    When logger_instance is None, operates in lightweight passthrough mode without
    accumulating or parsing chunks.
    """
    # Fast path: passthrough mode when no logger is provided
    if logger_instance is None:
        try:
            async for chunk_str in response_stream:
                if await request.is_disconnected():
                    logger.warning("Client disconnected, stopping stream.")
                    break
                yield chunk_str
        except Exception as e:
            logger.error(f"An error occurred during the response stream: {e}")
            # Yield a final error message to the client
            error_payload = {
                "error": {
                    "message": f"An unexpected error occurred during the stream: {str(e)}",
                    "type": "proxy_internal_error",
                    "code": 500,
                }
            }
            yield f"data: {json.dumps(error_payload)}\n\n"
            yield "data: [DONE]\n\n"
        return

    # Full aggregation mode when logging is enabled
    response_chunks = []
    full_response = {}

    try:
        async for chunk_str in response_stream:
            if await request.is_disconnected():
                logger.warning("Client disconnected, stopping stream.")
                break
            yield chunk_str
            if chunk_str.strip() and chunk_str.startswith("data:"):
                content = chunk_str[len("data:") :].strip()
                if content != "[DONE]":
                    try:
                        chunk_data = json.loads(content)
                        response_chunks.append(chunk_data)
                        logger_instance.log_stream_chunk(chunk_data)
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        logger.error(f"An error occurred during the response stream: {e}")
        # Yield a final error message to the client
        error_payload = {
            "error": {
                "message": f"An unexpected error occurred during the stream: {str(e)}",
                "type": "proxy_internal_error",
                "code": 500,
            }
        }
        yield f"data: {json.dumps(error_payload)}\n\n"
        yield "data: [DONE]\n\n"
        # Also log this as a failed request
        logger_instance.log_final_response(
            status_code=500, headers=None, body={"error": str(e)}
        )
        return
    finally:
        if response_chunks:
            full_response = _aggregate_streaming_chunks(response_chunks)

            logger_instance.log_final_response(
                status_code=200,
                headers=None,
                body=full_response,
            )


def _aggregate_streaming_chunks(chunks: list) -> dict:
    """
    Aggregate streaming chunks into a final response structure.

    Args:
        chunks: List of parsed chunk data

    Returns:
        Aggregated response dict
    """
    final_message = {"role": "assistant"}
    aggregated_tool_calls: Dict[int, dict] = {}
    usage_data = None
    finish_reason = None

    for chunk in chunks:
        if "choices" in chunk and chunk["choices"]:
            choice = chunk["choices"][0]
            delta = choice.get("delta", {})

            # Dynamically aggregate all fields from the delta
            for key, value in delta.items():
                if value is None:
                    continue

                if key == "content":
                    if "content" not in final_message:
                        final_message["content"] = ""
                    if value:
                        final_message["content"] += value

                elif key == "tool_calls":
                    for tc_chunk in value:
                        index = tc_chunk["index"]
                        if index not in aggregated_tool_calls:
                            aggregated_tool_calls[index] = {
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        # Ensure 'function' key exists
                        if "function" not in aggregated_tool_calls[index]:
                            aggregated_tool_calls[index]["function"] = {
                                "name": "",
                                "arguments": "",
                            }
                        if tc_chunk.get("id"):
                            aggregated_tool_calls[index]["id"] = tc_chunk["id"]
                        if "function" in tc_chunk:
                            if "name" in tc_chunk["function"]:
                                if tc_chunk["function"]["name"] is not None:
                                    aggregated_tool_calls[index]["function"][
                                        "name"
                                    ] += tc_chunk["function"]["name"]
                            if "arguments" in tc_chunk["function"]:
                                if tc_chunk["function"]["arguments"] is not None:
                                    aggregated_tool_calls[index]["function"][
                                        "arguments"
                                    ] += tc_chunk["function"]["arguments"]

                elif key == "function_call":
                    if "function_call" not in final_message:
                        final_message["function_call"] = {"name": "", "arguments": ""}
                    if "name" in value:
                        if value["name"] is not None:
                            final_message["function_call"]["name"] += value["name"]
                    if "arguments" in value:
                        if value["arguments"] is not None:
                            final_message["function_call"]["arguments"] += value[
                                "arguments"
                            ]

                else:  # Generic key handling
                    if key == "role":
                        final_message[key] = value
                    elif key not in final_message:
                        final_message[key] = value
                    elif isinstance(final_message.get(key), str):
                        final_message[key] += value
                    else:
                        final_message[key] = value

            if "finish_reason" in choice and choice["finish_reason"]:
                finish_reason = choice["finish_reason"]

        if "usage" in chunk and chunk["usage"]:
            usage_data = chunk["usage"]

    # Final Response Construction
    if aggregated_tool_calls:
        final_message["tool_calls"] = list(aggregated_tool_calls.values())
        # Override finish_reason when tool_calls exist
        finish_reason = "tool_calls"

    # Ensure standard fields are present
    for field in ["content", "tool_calls", "function_call"]:
        if field not in final_message:
            final_message[field] = None

    first_chunk = chunks[0]
    final_choice = {
        "index": 0,
        "message": final_message,
        "finish_reason": finish_reason,
    }

    return {
        "id": first_chunk.get("id"),
        "object": "chat.completion",
        "created": first_chunk.get("created"),
        "model": first_chunk.get("model"),
        "choices": [final_choice],
        "usage": usage_data,
    }
