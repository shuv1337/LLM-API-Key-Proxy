# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 Mirrowel

# src/rotator_library/utils/__init__.py

from .headless_detection import is_headless_environment
from .paths import (
    get_default_root,
    get_logs_dir,
    get_cache_dir,
    get_oauth_dir,
    get_data_file,
)
from .reauth_coordinator import get_reauth_coordinator, ReauthCoordinator
from .resilient_io import (
    BufferedWriteRegistry,
    ResilientStateWriter,
    safe_write_json,
    safe_log_write,
    safe_read_json,
    safe_mkdir,
)
from .openai_codex_jwt import (
    AUTH_CLAIM,
    ACCOUNT_ID_CLAIM,
    decode_jwt_unverified,
    extract_account_id_from_payload,
    extract_explicit_email_from_payload,
    extract_email_from_payload,
    extract_expiry_ms_from_payload,
)
from .suppress_litellm_warnings import suppress_litellm_serialization_warnings

__all__ = [
    "is_headless_environment",
    "get_default_root",
    "get_logs_dir",
    "get_cache_dir",
    "get_oauth_dir",
    "get_data_file",
    "get_reauth_coordinator",
    "ReauthCoordinator",
    "BufferedWriteRegistry",
    "ResilientStateWriter",
    "safe_write_json",
    "safe_log_write",
    "safe_read_json",
    "safe_mkdir",
    "AUTH_CLAIM",
    "ACCOUNT_ID_CLAIM",
    "decode_jwt_unverified",
    "extract_account_id_from_payload",
    "extract_explicit_email_from_payload",
    "extract_email_from_payload",
    "extract_expiry_ms_from_payload",
    "suppress_litellm_serialization_warnings",
]
