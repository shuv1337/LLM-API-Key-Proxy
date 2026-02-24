# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
FastAPI application factory.

This module provides the create_app() function for creating and configuring
the FastAPI application instance.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from proxy_app.startup import lifespan


def create_app(data_dir: Optional[Path] = None) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        data_dir: Optional data directory path

    Returns:
        Configured FastAPI application instance
    """
    # Create app with lifespan
    app = FastAPI(
        title="LLM API Key Proxy",
        description="A proxy server for LLM API key rotation and management",
        version="1.0.0",
        lifespan=lambda app: lifespan(app, data_dir),
    )

    # Configure CORS
    _configure_cors(app)

    # Register routes
    _register_routes(app)

    return app


def _configure_cors(app: FastAPI) -> None:
    """Configure CORS middleware from environment variables."""
    # PROXY_CORS_ORIGINS: comma-separated list or "*" for all
    _cors_origins_env = os.getenv("PROXY_CORS_ORIGINS", "*")
    _cors_origins = [origin.strip() for origin in _cors_origins_env.split(",") if origin.strip()]
    _cors_credentials = os.getenv("PROXY_CORS_CREDENTIALS", "false").lower() == "true"

    # Security warnings
    if _cors_origins == ["*"]:
        logging.warning(
            "CORS is configured to allow all origins (*). "
            "Set PROXY_CORS_ORIGINS to a specific domain list for production."
        )
    if _cors_credentials and _cors_origins == ["*"]:
        logging.warning(
            "CORS allow_credentials is enabled with wildcard origins. "
            "Browsers reject this combination. Set explicit PROXY_CORS_ORIGINS."
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=_cors_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _register_routes(app: FastAPI) -> None:
    """Register all API routes."""
    from proxy_app.routes import openai, anthropic, admin

    # OpenAI-compatible routes
    app.include_router(openai.router)

    # Anthropic-compatible routes
    app.include_router(anthropic.router)

    # Admin routes
    app.include_router(admin.router)
