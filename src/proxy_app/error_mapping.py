# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
Centralized error mapping from LiteLLM exceptions to FastAPI HTTPExceptions.

This module eliminates duplicated exception handling blocks across endpoints
by providing a single mapping function for LiteLLM errors.
"""

from fastapi import HTTPException
from typing import Optional, Dict, Any
import litellm
import logging

logger = logging.getLogger(__name__)


def map_litellm_error(e: Exception, context: Optional[str] = None) -> HTTPException:
    """
    Map a LiteLLM exception to an appropriate HTTPException.

    Args:
        e: The exception from LiteLLM or related libraries
        context: Optional context string for logging (e.g., endpoint name)

    Returns:
        HTTPException with appropriate status code and detail
    """
    ctx = f" ({context})" if context else ""

    # Map specific LiteLLM error types to HTTP status codes
    if isinstance(e, litellm.InvalidRequestError):
        return HTTPException(status_code=400, detail=f"Invalid Request: {str(e)}")

    if isinstance(e, ValueError):
        return HTTPException(status_code=400, detail=f"Invalid Request: {str(e)}")

    if isinstance(e, litellm.ContextWindowExceededError):
        return HTTPException(status_code=400, detail=f"Context Window Exceeded: {str(e)}")

    if isinstance(e, litellm.AuthenticationError):
        return HTTPException(status_code=401, detail=f"Authentication Error: {str(e)}")

    if isinstance(e, litellm.RateLimitError):
        return HTTPException(status_code=429, detail=f"Rate Limit Exceeded: {str(e)}")

    if isinstance(e, litellm.ServiceUnavailableError):
        return HTTPException(status_code=503, detail=f"Service Unavailable: {str(e)}")

    if isinstance(e, litellm.APIConnectionError):
        return HTTPException(status_code=503, detail=f"Service Unavailable: {str(e)}")

    if isinstance(e, litellm.Timeout):
        return HTTPException(status_code=504, detail=f"Gateway Timeout: {str(e)}")

    if isinstance(e, litellm.InternalServerError):
        return HTTPException(status_code=502, detail=f"Bad Gateway: {str(e)}")

    if isinstance(e, litellm.OpenAIError):
        return HTTPException(status_code=502, detail=f"Bad Gateway: {str(e)}")

    # Log unexpected errors
    logger.error(f"Unhandled exception{ctx}: {e}")
    return HTTPException(status_code=500, detail=str(e))


def create_anthropic_error_response(
    error_type: str, message: str, status_code: int
) -> Dict[str, Any]:
    """
    Create an Anthropic-compatible error response structure.

    Args:
        error_type: The error type string (e.g., 'invalid_request_error')
        message: The error message
        status_code: The HTTP status code

    Returns:
        Dict with Anthropic error format
    """
    return {
        "type": "error",
        "error": {"type": error_type, "message": message},
    }


def map_litellm_error_to_anthropic(
    e: Exception, context: Optional[str] = None
) -> HTTPException:
    """
    Map a LiteLLM exception to an Anthropic-compatible HTTPException.

    Args:
        e: The exception from LiteLLM or related libraries
        context: Optional context string for logging

    Returns:
        HTTPException with Anthropic-formatted error detail
    """
    ctx = f" ({context})" if context else ""

    error_response = None
    status_code = 500

    if isinstance(e, (litellm.InvalidRequestError, ValueError, litellm.ContextWindowExceededError)):
        error_response = create_anthropic_error_response(
            "invalid_request_error", str(e), 400
        )
        status_code = 400
    elif isinstance(e, litellm.AuthenticationError):
        error_response = create_anthropic_error_response(
            "authentication_error", str(e), 401
        )
        status_code = 401
    elif isinstance(e, litellm.RateLimitError):
        error_response = create_anthropic_error_response(
            "rate_limit_error", str(e), 429
        )
        status_code = 429
    elif isinstance(e, (litellm.ServiceUnavailableError, litellm.APIConnectionError)):
        error_response = create_anthropic_error_response("api_error", str(e), 503)
        status_code = 503
    elif isinstance(e, litellm.Timeout):
        error_response = create_anthropic_error_response(
            "api_error", f"Request timed out: {str(e)}", 504
        )
        status_code = 504
    else:
        # Default to api_error for unhandled exceptions
        logger.error(f"Unhandled exception in Anthropic endpoint{ctx}: {e}")
        error_response = create_anthropic_error_response("api_error", str(e), 500)
        status_code = 500

    return HTTPException(status_code=status_code, detail=error_response)


class ErrorMappingHelper:
    """
    Helper class for endpoints to handle common error patterns.

    Usage:
        error_helper = ErrorMappingHelper("chat_completions")
        try:
            ...
        except Exception as e:
            raise error_helper.handle_error(e)
    """

    def __init__(self, endpoint_name: str, mode: str = "openai"):
        """
        Initialize error mapping helper.

        Args:
            endpoint_name: Name of the endpoint for logging context
            mode: 'openai' or 'anthropic' error format
        """
        self.endpoint_name = endpoint_name
        self.mode = mode

    def handle_error(self, e: Exception) -> HTTPException:
        """Map exception to appropriate HTTPException."""
        if self.mode == "anthropic":
            return map_litellm_error_to_anthropic(e, self.endpoint_name)
        return map_litellm_error(e, self.endpoint_name)
