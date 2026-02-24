# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
Application startup and shutdown logic.

This module contains the lifespan context manager and initialization
code for the FastAPI application.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI

from rotator_library import RotatingClient, PROVIDER_PLUGINS
from rotator_library.credential_manager import CredentialManager
from rotator_library.background_refresher import BackgroundRefresher
from rotator_library.model_info_service import init_model_info_service
from proxy_app.batch_manager import EmbeddingBatcher

logger = logging.getLogger(__name__)


def _mask_api_key(key: str) -> str:
    """Mask API key for safe display in logs. Shows first 4 and last 4 chars."""
    if not key or len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


@asynccontextmanager
async def lifespan(app: FastAPI, data_dir: Optional[Path] = None):
    """
    Manage the RotatingClient's lifecycle with the app's lifespan.

    Args:
        app: The FastAPI application instance
        data_dir: Optional data directory path
    """
    from rotator_library.utils.paths import get_default_root

    root_dir = data_dir or get_default_root()

    # Perform skippable OAuth initialization at startup
    skip_oauth_init = os.getenv("SKIP_OAUTH_INIT_CHECK", "false").lower() == "true"

    # Credential discovery
    cred_manager = CredentialManager(os.environ)
    oauth_credentials = cred_manager.discover_and_prepare()

    if not skip_oauth_init and oauth_credentials:
        oauth_credentials = await _process_oauth_credentials(oauth_credentials)

    # Load provider-specific params
    litellm_provider_params = {
        "gemini_cli": {"project_id": os.getenv("GEMINI_CLI_PROJECT_ID")}
    }

    # Load global timeout
    global_timeout = int(os.getenv("GLOBAL_TIMEOUT", "30"))

    # Build API keys dict
    api_keys = _discover_api_keys()

    # Load model filters
    ignore_models = _load_model_filters("IGNORE_MODELS_")
    whitelist_models = _load_model_filters("WHITELIST_MODELS_")

    # Load max concurrent per key
    max_concurrent = _load_max_concurrent()

    # Initialize client
    client = RotatingClient(
        api_keys=api_keys,
        oauth_credentials=oauth_credentials,
        configure_logging=True,
        global_timeout=global_timeout,
        litellm_provider_params=litellm_provider_params,
        ignore_models=ignore_models,
        whitelist_models=whitelist_models,
        enable_request_logging=os.getenv("ENABLE_REQUEST_LOGGING", "false").lower() == "true",
        max_concurrent_requests_per_key=max_concurrent,
    )

    await client.initialize_usage_managers()

    # Start background refresher
    client.background_refresher.start()
    app.state.rotating_client = client

    # Warn if no credentials
    if not client.all_credentials:
        logging.warning("=" * 70)
        logging.warning("⚠️  NO PROVIDER CREDENTIALS CONFIGURED")
        logging.warning("The proxy is running but cannot serve any LLM requests.")
        logging.warning("Launch the credential tool to add API keys or OAuth credentials.")
        logging.warning("  • Executable: Run with --add-credential flag")
        logging.warning("  • Source: python src/proxy_app/main.py --add-credential")
        logging.warning("=" * 70)

    # Initialize embedding batcher
    USE_EMBEDDING_BATCHER = os.getenv("USE_EMBEDDING_BATCHER", "false").lower() == "true"
    if USE_EMBEDDING_BATCHER:
        batcher = EmbeddingBatcher(client=client)
        app.state.embedding_batcher = batcher
        logging.info("RotatingClient and EmbeddingBatcher initialized.")
    else:
        app.state.embedding_batcher = None
        logging.info("RotatingClient initialized (EmbeddingBatcher disabled).")

    # Start model info service
    model_info_service = await init_model_info_service()
    app.state.model_info_service = model_info_service
    logging.info("Model info service started (fetching pricing data in background).")

    yield

    # Shutdown
    await client.background_refresher.stop()
    if app.state.embedding_batcher:
        await app.state.embedding_batcher.stop()
    await client.close()

    if app.state.embedding_batcher:
        logging.info("RotatingClient and EmbeddingBatcher closed.")
    else:
        logging.info("RotatingClient closed.")

    # Stop model info service
    if hasattr(app.state, "model_info_service") and app.state.model_info_service:
        await app.state.model_info_service.stop()


async def _process_oauth_credentials(
    oauth_credentials: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Process OAuth credentials with deduplication."""
    processed_emails: Dict[str, Dict[str, str]] = {}
    credentials_to_initialize: Dict[str, List[str]] = {}
    final_oauth_credentials: Dict[str, List[str]] = {}

    logging.info("Starting OAuth credential validation and deduplication...")

    # Pass 1: Pre-scan for duplicates
    for provider, paths in oauth_credentials.items():
        if provider not in credentials_to_initialize:
            credentials_to_initialize[provider] = []
        for path in paths:
            if path.startswith("env://"):
                credentials_to_initialize[provider].append(path)
                continue

            email, _ = await _read_credential_metadata(path)

            if email:
                if email not in processed_emails:
                    processed_emails[email] = {}

                if provider in processed_emails[email]:
                    original_path = processed_emails[email][provider]
                    logging.warning(
                        f"Duplicate for '{email}' on '{provider}' found in pre-scan: "
                        f"'{Path(path).name}'. Original: '{Path(original_path).name}'. Skipping."
                    )
                    continue
                else:
                    processed_emails[email][provider] = path
                credentials_to_initialize[provider].append(path)
            elif email is None:
                logging.warning(
                    f"Could not pre-read metadata from '{path}'. Will process during initialization."
                )
                credentials_to_initialize[provider].append(path)

    # Pass 2: Parallel initialization
    async def process_credential(provider: str, path: str, provider_instance):
        """Process a single credential: initialize and fetch user info."""
        try:
            await provider_instance.initialize_token(path)

            if not hasattr(provider_instance, "get_user_info"):
                return (provider, path, None, None)

            user_info = await provider_instance.get_user_info(path)
            email = user_info.get("email")
            return (provider, path, email, None)

        except Exception as e:
            logging.error(f"Failed to process OAuth token for {provider} at '{path}': {e}")
            return (provider, path, None, e)

    tasks = []
    for provider, paths in credentials_to_initialize.items():
        if not paths:
            continue

        provider_plugin_class = PROVIDER_PLUGINS.get(provider)
        if not provider_plugin_class:
            continue

        provider_instance = provider_plugin_class()

        for path in paths:
            tasks.append(process_credential(provider, path, provider_instance))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Pass 3: Sequential deduplication and final assembly
    for result in results:
        if isinstance(result, Exception):
            logging.error(f"Credential processing raised exception: {result}")
            continue

        provider, path, email, error = result

        if error:
            continue

        if email is None:
            if provider not in final_oauth_credentials:
                final_oauth_credentials[provider] = []
            final_oauth_credentials[provider].append(path)
            continue

        if not email:
            logging.warning(f"Could not retrieve email for '{path}'. Treating as unique.")
            if provider not in final_oauth_credentials:
                final_oauth_credentials[provider] = []
            final_oauth_credentials[provider].append(path)
            continue

        # Deduplication check
        if email not in processed_emails:
            processed_emails[email] = {}

        if provider in processed_emails[email] and processed_emails[email][provider] != path:
            original_path = processed_emails[email][provider]
            logging.warning(
                f"Duplicate for '{email}' on '{provider}' found post-init: "
                f"'{Path(path).name}'. Original: '{Path(original_path).name}'. Skipping."
            )
            continue
        else:
            processed_emails[email][provider] = path
            if provider not in final_oauth_credentials:
                final_oauth_credentials[provider] = []
            final_oauth_credentials[provider].append(path)

            # Update metadata
            if not path.startswith("env://"):
                await _update_metadata_file(path, email)

    logging.info("OAuth credential processing complete.")
    return final_oauth_credentials


async def _read_credential_metadata(path: str) -> tuple:
    """Read credential file and extract email from metadata."""
    try:
        def _read_file():
            with open(path, "r") as f:
                return json.load(f)
        data = await asyncio.to_thread(_read_file)
        metadata = data.get("_proxy_metadata", {})
        return metadata.get("email"), data
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None


async def _update_metadata_file(path: str, email: str):
    """Update credential metadata file with email and timestamp."""
    try:
        def _do_update():
            with open(path, "r+") as f:
                data = json.load(f)
                metadata = data.get("_proxy_metadata", {})
                metadata["email"] = email
                metadata["last_check_timestamp"] = time.time()
                data["_proxy_metadata"] = metadata
                f.seek(0)
                json.dump(data, f, indent=2)
                f.truncate()
        await asyncio.to_thread(_do_update)
    except Exception as e:
        logging.error(f"Failed to update metadata for '{path}': {e}")


def _discover_api_keys() -> Dict[str, List[str]]:
    """Discover API keys from environment variables."""
    api_keys = {}
    for key, value in os.environ.items():
        if "_API_KEY" in key and key != "PROXY_API_KEY":
            provider = key.split("_API_KEY")[0].lower()
            if provider not in api_keys:
                api_keys[provider] = []
            api_keys[provider].append(value)
    return api_keys


def _load_model_filters(prefix: str) -> Dict[str, List[str]]:
    """Load model filters from environment variables."""
    filters = {}
    for key, value in os.environ.items():
        if key.startswith(prefix):
            provider = key.replace(prefix, "").lower()
            models = [model.strip() for model in value.split(",") if model.strip()]
            filters[provider] = models
    return filters


def _load_max_concurrent() -> Dict[str, int]:
    """Load max concurrent requests per key from environment."""
    max_concurrent = {}
    for key, value in os.environ.items():
        if key.startswith("MAX_CONCURRENT_REQUESTS_PER_KEY_"):
            provider = key.replace("MAX_CONCURRENT_REQUESTS_PER_KEY_", "").lower()
            try:
                max_concurrent[provider] = max(1, int(value))
            except ValueError:
                logging.warning(f"Invalid max_concurrent for '{provider}': {value}")
    return max_concurrent
