# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 Mirrowel

"""Shared JWT parsing helpers for OpenAI Codex OAuth credentials.

These helpers intentionally decode JWT payloads without signature verification.
They are only used for non-authoritative metadata extraction (account/email/exp),
not for auth decisions.
"""

import base64
import json
from typing import Any, Dict, Optional

AUTH_CLAIM = "https://api.openai.com/auth"
ACCOUNT_ID_CLAIM = "https://api.openai.com/auth.chatgpt_account_id"


def decode_jwt_unverified(token: str) -> Optional[Dict[str, Any]]:
    """Decode JWT payload without signature verification."""
    if not token or not isinstance(token, str):
        return None

    parts = token.split(".")
    if len(parts) < 2:
        return None

    payload_segment = parts[1]
    padding = "=" * (-len(payload_segment) % 4)

    try:
        payload_bytes = base64.urlsafe_b64decode(payload_segment + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def extract_account_id_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extract account ID from known OpenAI Codex JWT claim locations."""
    if not payload:
        return None

    # 1) Direct dotted claim format
    direct = payload.get(ACCOUNT_ID_CLAIM)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    # 2) Nested object claim format observed in real tokens
    auth_claim = payload.get(AUTH_CLAIM)
    if isinstance(auth_claim, dict):
        nested = auth_claim.get("chatgpt_account_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()

    # 3) Fallback organizations[0].id if present
    orgs = payload.get("organizations")
    if isinstance(orgs, list) and orgs:
        first = orgs[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id.strip():
                return org_id.strip()

    return None


def extract_explicit_email_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extract explicit email claim only (no subject fallback)."""
    if not payload:
        return None

    email = payload.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()

    return None


def extract_email_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extract email fallback chain: email -> sub."""
    if not payload:
        return None

    email = extract_explicit_email_from_payload(payload)
    if email:
        return email

    sub = payload.get("sub")
    if isinstance(sub, str) and sub.strip():
        return sub.strip()

    return None


def extract_expiry_ms_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[int]:
    """Extract JWT exp claim and convert to milliseconds."""
    if not payload:
        return None

    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(float(exp) * 1000)

    return None
