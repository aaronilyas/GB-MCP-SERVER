"""Application identity for MCP tools. Transport auth lives in gb_mcp.http."""

from __future__ import annotations

from typing import Any

import db
from gb_mcp.http import current_oauth_identity

_EMAIL_INSTRUCTION = (
    "Provide the email address of the user of the LLM. Ask the user for this "
    "if you do not already have it. Do not invent an email."
)


def token_identity_email() -> str | None:
    """Canonical email from the current OAuth access token email/sub claim."""
    raw = current_oauth_identity()
    if raw is None:
        return None
    try:
        return db.normalize_email(raw)
    except ValueError:
        return None


def model_request() -> dict[str, Any]:
    """Single missing-identity payload. Tools must not invent an email."""
    return {
        "ok": False,
        "model_request": {
            "name": "email",
            "instruction": _EMAIL_INSTRUCTION,
        },
    }


def require_email() -> str | dict[str, Any]:
    """Return the bound email, or ``model_request()`` if none is available."""
    email = token_identity_email()
    if email is None:
        return model_request()
    return email
