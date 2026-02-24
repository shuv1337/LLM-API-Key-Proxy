# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
Admin and utility API routes.

This module contains administrative endpoints including:
- Quota stats (/v1/quota-stats)
- Provider list (/v1/providers)
- Model info stats (/v1/model-info/stats)
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends

from rotator_library import RotatingClient

from proxy_app.dependencies import (
    get_rotating_client,
    get_model_info_service,
    verify_api_key,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/v1/providers")
async def list_providers(_=Depends(verify_api_key)):
    """Returns a list of all available providers."""
    from rotator_library.providers import PROVIDER_PLUGINS
    return list(PROVIDER_PLUGINS.keys())


@router.get("/v1/quota-stats")
async def get_quota_stats(
    request: Request,
    client: RotatingClient = Depends(get_rotating_client),
    _=Depends(verify_api_key),
    provider: Optional[str] = None,
):
    """
    Returns quota and usage statistics for all credentials.

    This returns cached data from the proxy without making external API calls.
    Use POST to reload from disk or force refresh from external APIs.
    """
    try:
        stats = await client.get_quota_stats(provider_filter=provider)
        return stats
    except Exception as e:
        logger.error(f"Failed to get quota stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/v1/quota-stats")
async def refresh_quota_stats(
    request: Request,
    client: RotatingClient = Depends(get_rotating_client),
    _=Depends(verify_api_key),
):
    """
    Refresh quota and usage statistics.

    Request body:
        {
            "action": "reload" | "force_refresh",
            "scope": "all" | "provider" | "credential",
            "provider": "antigravity",
            "credential": "antigravity_oauth_1.json"
        }
    """
    try:
        data = await request.json()
        action = data.get("action", "reload")
        scope = data.get("scope", "all")
        provider = data.get("provider")
        credential = data.get("credential")

        # Validate parameters
        if action not in ("reload", "force_refresh"):
            raise HTTPException(
                status_code=400,
                detail="action must be 'reload' or 'force_refresh'",
            )

        if scope not in ("all", "provider", "credential"):
            raise HTTPException(
                status_code=400,
                detail="scope must be 'all', 'provider', or 'credential'",
            )

        if scope in ("provider", "credential") and not provider:
            raise HTTPException(
                status_code=400,
                detail="'provider' is required when scope is 'provider' or 'credential'",
            )

        if scope == "credential" and not credential:
            raise HTTPException(
                status_code=400,
                detail="'credential' is required when scope is 'credential'",
            )

        refresh_result = {
            "action": action,
            "scope": scope,
            "provider": provider,
            "credential": credential,
        }

        if action == "reload":
            # Just reload from disk
            start_time = time.time()
            await client.reload_usage_from_disk()
            refresh_result["duration_ms"] = int((time.time() - start_time) * 1000)
            refresh_result["success"] = True
            refresh_result["message"] = "Reloaded usage data from disk"

        elif action == "force_refresh":
            # Force refresh from external API
            result = await client.force_refresh_quota(
                provider=provider if scope in ("provider", "credential") else None,
                credential=credential if scope == "credential" else None,
            )
            refresh_result.update(result)
            refresh_result["success"] = result["failed_count"] == 0

        # Get updated stats
        stats = await client.get_quota_stats(provider_filter=provider)
        stats["refresh_result"] = refresh_result
        stats["data_source"] = "refreshed"

        return stats

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to refresh quota stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/v1/model-info/stats")
async def model_info_stats(
    request: Request,
    _=Depends(verify_api_key),
):
    """Returns statistics about the model info service (for monitoring/debugging)."""
    model_info_service = get_model_info_service(request)
    if model_info_service:
        return model_info_service.get_stats()
    return {"error": "Model info service not initialized"}
