# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
FastAPI dependencies for the proxy application.

This module centralizes all FastAPI dependency functions including:
- Credential retrieval from app state
- API key verification for OpenAI and Anthropic endpoints
"""

import os
from typing import Optional

from fastapi import Request, HTTPException, Depends
from fastapi.security import APIKeyHeader

from rotator_library import RotatingClient
from proxy_app.batch_manager import EmbeddingBatcher

# Configuration
PROXY_API_KEY = os.getenv("PROXY_API_KEY")

# Security schemes
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)
anthropic_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


def get_rotating_client(request: Request) -> RotatingClient:
    """Dependency to get the rotating client instance from the app state."""
    return request.app.state.rotating_client


def get_embedding_batcher(request: Request) -> Optional[EmbeddingBatcher]:
    """Dependency to get the embedding batcher instance from the app state."""
    return getattr(request.app.state, "embedding_batcher", None)


def get_model_info_service(request: Request):
    """Dependency to get the model info service from the app state."""
    return getattr(request.app.state, "model_info_service", None)


async def verify_api_key(auth: str = Depends(api_key_header)):
    """
    Dependency to verify the proxy API key for OpenAI-compatible endpoints.

    If PROXY_API_KEY is not set, skips verification (open access mode).
    Accepts Bearer token in Authorization header.
    """
    # If PROXY_API_KEY is not set or empty, skip verification (open access)
    if not PROXY_API_KEY:
        return auth
    if not auth or auth != f"Bearer {PROXY_API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    return auth


async def verify_anthropic_api_key(
    x_api_key: str = Depends(anthropic_api_key_header),
    auth: str = Depends(api_key_header),
):
    """
    Dependency to verify API key for Anthropic endpoints.

    Accepts either x-api-key header (Anthropic style) or Authorization Bearer (OpenAI style).
    If PROXY_API_KEY is not set, skips verification (open access mode).
    """
    # Check x-api-key first (Anthropic style)
    if x_api_key and x_api_key == PROXY_API_KEY:
        return x_api_key
    # Fall back to Bearer token (OpenAI style)
    if auth and auth == f"Bearer {PROXY_API_KEY}":
        return auth
    # If PROXY_API_KEY is not set, skip verification (open access)
    if not PROXY_API_KEY:
        return x_api_key or auth
    raise HTTPException(status_code=401, detail="Invalid or missing API Key")


def require_api_key():
    """Factory for API key dependency - always requires key (strict mode)."""
    if not PROXY_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="PROXY_API_KEY must be configured for this endpoint"
        )
    return Depends(verify_api_key)
