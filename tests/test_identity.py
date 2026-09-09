from __future__ import annotations

from gb_mcp.http import oauth_token_claims
from gb_mcp.identity import require_email


def test_require_email_binds_oauth_email_claim() -> None:
    with oauth_token_claims({"email": "Owner@Example.com"}):
        assert require_email() == "owner@example.com"
        assert require_email(explicit=None) == "owner@example.com"


def test_require_email_explicit_overrides_token() -> None:
    with oauth_token_claims({"email": "token@example.com"}):
        assert require_email(explicit="Owner@Example.com") == "owner@example.com"


def test_require_email_omitted_without_token_returns_model_request() -> None:
    result = require_email()
    assert result["ok"] is False
    assert result["model_request"]["name"] == "email"
    assert "email" in result["model_request"]["instruction"].lower()


def test_require_email_non_email_sub_is_not_identity() -> None:
    with oauth_token_claims({"sub": "gb-mcp-user"}):
        result = require_email()
    assert "model_request" in result
