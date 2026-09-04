"""In-process OAuth 2.1 authorization server for Streamable HTTP MCP.

Uses MCP SDK 2.1.1 primitives (provider protocol, DCR, token endpoint, PKCE)
on the same origin as the resource server. Issuer and resource URLs are
supplied per request by `gb_mcp.http` — never hard-coded.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import jwt
from jwt.exceptions import PyJWTError
from pydantic import AnyUrl, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mcp.server.auth.errors import stringify_pydantic_error
from mcp.server.auth.handlers.authorize import AuthorizationRequest
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import (
    InvalidRedirectUriError,
    InvalidScopeError,
    OAuthClientInformationFull,
    OAuthToken,
)
from mcp.shared.auth_utils import check_resource_allowed

from gb_mcp import config

MCP_SCOPE = "mcp"
SCOPES_SUPPORTED = [MCP_SCOPE]
RESOURCE_NAME = "gb-mcp-server"
_JWT_ALGORITHMS = ["HS256"]
_ACCESS_TTL_SECONDS = 3600
_REFRESH_TTL_SECONDS = 7 * 24 * 3600
_CODE_TTL_SECONDS = 300
_CONSENT_TTL_SECONDS = 600
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REGISTRATION_OPTIONS = ClientRegistrationOptions(
    enabled=True,
    valid_scopes=SCOPES_SUPPORTED,
    default_scopes=SCOPES_SUPPORTED,
)


class IssuedAuthorizationCode(AuthorizationCode):
    issuer: str | None = None


class IssuedRefreshToken(RefreshToken):
    resource: str | None = None
    issuer: str | None = None


@dataclass
class _PendingConsent:
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    state: str | None
    scopes: list[str]
    resource: str
    issuer: str
    expires_at: float


class GbMcpOAuthProvider(
    OAuthAuthorizationServerProvider[IssuedAuthorizationCode, IssuedRefreshToken, AccessToken]
):
    """Authorization-code + PKCE S256 provider with rotating refresh tokens."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, IssuedAuthorizationCode] = {}
        self._refresh: dict[str, IssuedRefreshToken] = {}
        self._pending: dict[str, _PendingConsent] = {}

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()
            self._codes.clear()
            self._refresh.clear()
            self._pending.clear()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not redirect_uri_allowed(str(uri)):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=(
                        f"Redirect URI '{uri}' is not allowed. Use http loopback "
                        "(127.0.0.1 / localhost) or an https URI."
                    ),
                )
        if not client_info.client_id:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="client_id is required",
            )
        with self._lock:
            self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        raise AuthorizeError(
            error="server_error",
            error_description="Authorization is completed on the consent page",
        )

    def store_pending(self, pending: _PendingConsent) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._pending[token] = pending
        return token

    def take_pending(self, consent_token: str) -> _PendingConsent | None:
        now = time.time()
        with self._lock:
            pending = self._pending.pop(consent_token, None)
        if pending is None or pending.expires_at < now:
            return None
        return pending

    def store_code(self, auth_code: IssuedAuthorizationCode) -> None:
        with self._lock:
            self._codes[auth_code.code] = auth_code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> IssuedAuthorizationCode | None:
        with self._lock:
            code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: IssuedAuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            stored = self._codes.pop(authorization_code.code, None)
            if stored is None or stored.client_id != client.client_id:
                raise TokenError(error="invalid_grant", error_description="authorization code does not exist")
            return self._issue_tokens_locked(
                client_id=client.client_id,
                scopes=stored.scopes,
                resource=stored.resource,
                issuer=stored.issuer,
                subject=stored.subject or "gb-mcp-user",
            )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> IssuedRefreshToken | None:
        with self._lock:
            token = self._refresh.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: IssuedRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with self._lock:
            stored = self._refresh.pop(refresh_token.token, None)
            if stored is None or stored.client_id != client.client_id:
                raise TokenError(error="invalid_grant", error_description="refresh token does not exist")
            return self._issue_tokens_locked(
                client_id=client.client_id,
                scopes=scopes,
                resource=stored.resource,
                issuer=stored.issuer,
                subject=stored.subject or "gb-mcp-user",
            )

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = decode_access_token_claims(token)
        if claims is None:
            return None
        scopes = _scopes_from_claim(claims.get("scope"))
        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or ""),
            scopes=scopes,
            expires_at=int(expires_at) if isinstance(expires_at, int) else None,
            resource=_audience_as_str(claims.get("aud")),
            subject=str(claims["sub"]) if claims.get("sub") is not None else None,
            claims=claims,
        )

    async def revoke_token(self, token: AccessToken | IssuedRefreshToken) -> None:
        if isinstance(token, IssuedRefreshToken) or isinstance(token, RefreshToken):
            with self._lock:
                self._refresh.pop(token.token, None)

    def _issue_tokens_locked(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        issuer: str | None,
        subject: str,
    ) -> OAuthToken:
        now = int(time.time())
        secret = config.token_signing_secret()
        if not secret:
            raise TokenError(error="server_error", error_description="token signing secret is not configured")
        if not issuer or not resource:
            raise TokenError(error="invalid_target", error_description="resource is required")
        granted = scopes or list(SCOPES_SUPPORTED)
        access = jwt.encode(
            {
                "iss": issuer,
                "aud": resource,
                "exp": now + _ACCESS_TTL_SECONDS,
                "iat": now,
                "nbf": now,
                "sub": subject,
                "scope": " ".join(granted),
                "client_id": client_id,
                "jti": secrets.token_urlsafe(16),
            },
            secret,
            algorithm="HS256",
        )
        refresh_value = secrets.token_urlsafe(48)
        self._refresh[refresh_value] = IssuedRefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=granted,
            expires_at=now + _REFRESH_TTL_SECONDS,
            subject=subject,
            resource=resource,
            issuer=issuer,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=_ACCESS_TTL_SECONDS,
            scope=" ".join(granted),
            refresh_token=refresh_value,
        )


_provider: GbMcpOAuthProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> GbMcpOAuthProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = GbMcpOAuthProvider()
        return _provider


def reset_oauth_state() -> None:
    get_provider().reset()


def redirect_uri_allowed(uri: str) -> bool:
    """Accept loopback http callbacks and https callbacks; never use startswith."""
    parsed = urlparse(uri)
    if parsed.fragment or parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if parsed.scheme == "http":
        return host in _LOOPBACK_HOSTS
    return True


def authorization_server_payload(issuer: str) -> dict[str, Any]:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "registration_endpoint": f"{issuer}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(SCOPES_SUPPORTED),
    }


def protected_resource_fields(resource: str, issuer: str) -> dict[str, Any]:
    return {
        "resource": resource,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": list(SCOPES_SUPPORTED),
        "resource_name": RESOURCE_NAME,
    }


def decode_access_token_claims(token: str) -> dict[str, Any] | None:
    secret = config.token_signing_secret()
    if not secret:
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=_JWT_ALGORITHMS,
            options={"verify_aud": False},
        )
    except PyJWTError:
        return None
    return claims if isinstance(claims, dict) else None


def oauth_claims_match_request(
    claims: dict[str, Any],
    *,
    issuer: str,
    resource: str,
) -> bool:
    token_iss = claims.get("iss")
    if not isinstance(token_iss, str) or not _issuers_equivalent(token_iss, issuer):
        return False
    audiences = _audience_values(claims.get("aud"))
    if resource in audiences:
        return True
    return any(same_resource(audience, resource) for audience in audiences)


def same_resource(requested: str, expected: str) -> bool:
    """True when two resource URLs identify this MCP server.

    Exact RFC 8707 hierarchical match first. Hosted connectors often send the
    origin they pasted (with or without ``www``, with or without ``/mcp``);
    those are the same resource as the canonical MCP URL on this site.
    """
    if check_resource_allowed(requested, expected) and check_resource_allowed(expected, requested):
        return True
    left = _resource_origin_key(requested)
    right = _resource_origin_key(expected)
    if left is None or right is None or left != right:
        return False
    return _resource_path_key(requested) == _resource_path_key(expected)


async def handle_authorize(request: Request, *, issuer: str, resource: str) -> Response:
    provider = get_provider()
    if request.method == "POST":
        form = await request.form()
        consent_token = form.get("consent_token")
        if isinstance(consent_token, str) and consent_token:
            return await _complete_consent(provider, form, consent_token)

    params = request.query_params if request.method == "GET" else await request.form()
    return await _begin_consent(provider, params, issuer=issuer, resource=resource)


async def handle_token(request: Request, *, resource: str) -> Response:
    provider = get_provider()
    form = await request.form()
    requested_resource = form.get("resource")
    grant_type = form.get("grant_type")
    if isinstance(requested_resource, str) and requested_resource:
        mismatch = await _token_resource_mismatch(
            provider,
            grant_type=grant_type if isinstance(grant_type, str) else None,
            code=form.get("code") if isinstance(form.get("code"), str) else None,
            refresh_token=form.get("refresh_token") if isinstance(form.get("refresh_token"), str) else None,
            client_id=form.get("client_id") if isinstance(form.get("client_id"), str) else None,
            requested_resource=requested_resource,
            expected_resource=resource,
        )
        if mismatch is not None:
            return mismatch
    handler = TokenHandler(provider, ClientAuthenticator(provider))
    return await handler.handle(request)


async def handle_register(request: Request) -> Response:
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        payload.setdefault("token_endpoint_auth_method", "none")
        request._body = json.dumps(payload).encode("utf-8")
    handler = RegistrationHandler(get_provider(), options=_REGISTRATION_OPTIONS)
    return await handler.handle(request)


async def _begin_consent(
    provider: GbMcpOAuthProvider,
    params: Any,
    *,
    issuer: str,
    resource: str,
) -> Response:
    state = _string_param(params, "state")
    try:
        auth_request = AuthorizationRequest.model_validate(params)
    except ValidationError as exc:
        return await _authorize_error(
            provider,
            params,
            error=_authorize_error_code(exc),
            error_description=stringify_pydantic_error(exc),
            state=state,
        )

    client = await provider.get_client(auth_request.client_id)
    if client is None:
        return _json_authorize_error(
            "invalid_request",
            f"Client ID '{auth_request.client_id}' not found",
            state=auth_request.state,
        )

    try:
        redirect_uri = client.validate_redirect_uri(auth_request.redirect_uri)
    except InvalidRedirectUriError as exc:
        return _json_authorize_error("invalid_request", exc.message, state=auth_request.state)

    try:
        scopes = client.validate_scope(auth_request.scope)
    except InvalidScopeError as exc:
        return _redirect_authorize_error(
            str(redirect_uri),
            error="invalid_scope",
            error_description=exc.message,
            state=auth_request.state,
        )
    if not scopes:
        scopes = list(SCOPES_SUPPORTED)

    bound_resource = resource
    if auth_request.resource:
        if not same_resource(auth_request.resource, resource):
            return _redirect_authorize_error(
                str(redirect_uri),
                error="invalid_target",
                error_description="resource does not match this MCP server",
                state=auth_request.state,
            )
        bound_resource = resource

    consent_token = provider.store_pending(
        _PendingConsent(
            client_id=client.client_id,
            redirect_uri=str(redirect_uri),
            redirect_uri_provided_explicitly=auth_request.redirect_uri is not None,
            code_challenge=auth_request.code_challenge,
            state=auth_request.state,
            scopes=scopes,
            resource=bound_resource,
            issuer=issuer,
            expires_at=time.time() + _CONSENT_TTL_SECONDS,
        )
    )
    return HTMLResponse(
        _consent_page(client=client, resource=bound_resource, redirect_uri=str(redirect_uri), consent_token=consent_token),
        headers={"Cache-Control": "no-store"},
    )


async def _complete_consent(provider: GbMcpOAuthProvider, form: Any, consent_token: str) -> Response:
    pending = provider.take_pending(consent_token)
    if pending is None:
        return _json_authorize_error("invalid_request", "consent request is invalid or expired")
    decision = _string_param(form, "consent")
    if decision != "allow":
        return _redirect_authorize_error(
            pending.redirect_uri,
            error="access_denied",
            error_description="The resource owner denied the request",
            state=pending.state,
        )
    code = secrets.token_urlsafe(32)
    provider.store_code(
        IssuedAuthorizationCode(
            code=code,
            scopes=pending.scopes,
            expires_at=time.time() + _CODE_TTL_SECONDS,
            client_id=pending.client_id,
            code_challenge=pending.code_challenge,
            redirect_uri=AnyUrl(pending.redirect_uri),
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource,
            subject="gb-mcp-user",
            issuer=pending.issuer,
        )
    )
    return RedirectResponse(
        url=construct_redirect_uri(pending.redirect_uri, code=code, state=pending.state),
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


async def _token_resource_mismatch(
    provider: GbMcpOAuthProvider,
    *,
    grant_type: str | None,
    code: str | None,
    refresh_token: str | None,
    client_id: str | None,
    requested_resource: str,
    expected_resource: str,
) -> Response | None:
    bound: str | None = None
    if grant_type == "authorization_code" and code and client_id:
        client = await provider.get_client(client_id)
        if client is not None:
            auth_code = await provider.load_authorization_code(client, code)
            if auth_code is not None:
                bound = auth_code.resource
    elif grant_type == "refresh_token" and refresh_token and client_id:
        client = await provider.get_client(client_id)
        if client is not None:
            token = await provider.load_refresh_token(client, refresh_token)
            if token is not None:
                bound = token.resource
    if bound is None:
        bound = expected_resource
    if not same_resource(requested_resource, bound):
        return JSONResponse(
            {
                "error": "invalid_target",
                "error_description": "resource does not match the authorization request",
            },
            status_code=400,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    return None


async def _authorize_error(
    provider: GbMcpOAuthProvider,
    params: Any,
    *,
    error: str,
    error_description: str,
    state: str | None,
) -> Response:
    client_id = _string_param(params, "client_id")
    client = await provider.get_client(client_id) if client_id else None
    redirect_uri = None
    if client is not None:
        try:
            raw = _string_param(params, "redirect_uri")
            redirect_uri = client.validate_redirect_uri(AnyUrl(raw) if raw else None)
        except (InvalidRedirectUriError, ValidationError):
            redirect_uri = None
    if redirect_uri is not None:
        return _redirect_authorize_error(
            str(redirect_uri),
            error=error,
            error_description=error_description,
            state=state,
        )
    return _json_authorize_error(error, error_description, state=state)


def _redirect_authorize_error(
    redirect_uri: str,
    *,
    error: str,
    error_description: str,
    state: str | None,
) -> RedirectResponse:
    return RedirectResponse(
        url=construct_redirect_uri(
            redirect_uri,
            error=error,
            error_description=error_description,
            state=state,
        ),
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


def _json_authorize_error(error: str, error_description: str, state: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": error, "error_description": error_description}
    if state is not None:
        body["state"] = state
    return JSONResponse(body, status_code=400, headers={"Cache-Control": "no-store"})


def _authorize_error_code(exc: ValidationError) -> str:
    for err in exc.errors():
        if err.get("loc") == ("response_type",) and err.get("type") == "literal_error":
            return "unsupported_response_type"
        if err.get("loc") == ("code_challenge_method",):
            return "invalid_request"
    return "invalid_request"


def _consent_page(
    *,
    client: OAuthClientInformationFull,
    resource: str,
    redirect_uri: str,
    consent_token: str,
) -> str:
    client_name = html.escape(client.client_name or client.client_id)
    resource_h = html.escape(resource)
    redirect_h = html.escape(redirect_uri)
    token_h = html.escape(consent_token)
    parsed = urlparse(redirect_uri)
    callback_host = html.escape(parsed.netloc or parsed.path)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Allow access to gb-mcp-server</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 36rem; margin: 2rem auto; padding: 0 1rem; color: #111; }}
    main {{ border: 1px solid #ccc; border-radius: 8px; padding: 1.25rem 1.5rem; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 0.75rem; }}
    p {{ line-height: 1.45; }}
    code {{ word-break: break-all; }}
    .actions {{ display: flex; gap: 0.75rem; margin-top: 1.25rem; }}
    button {{ font: inherit; padding: 0.5rem 1rem; cursor: pointer; }}
    button[value="allow"] {{ background: #0b57d0; color: #fff; border: 0; border-radius: 4px; }}
    button[value="deny"] {{ background: #fff; color: #111; border: 1px solid #888; border-radius: 4px; }}
  </style>
</head>
<body>
  <main>
    <h1>Allow access to this MCP server?</h1>
    <p><strong>{client_name}</strong> wants to connect to the Game Boy MCP resource.</p>
    <p>Resource: <code>{resource_h}</code></p>
    <p>After you allow, the browser returns to <code>{callback_host}</code> (<code>{redirect_h}</code>).</p>
    <p>You do not need a bearer token, password, or API key. Email used by tools is separate from this consent.</p>
    <form method="post" action="/authorize">
      <input type="hidden" name="consent_token" value="{token_h}">
      <div class="actions">
        <button type="submit" name="consent" value="allow">Allow</button>
        <button type="submit" name="consent" value="deny">Deny</button>
      </div>
    </form>
  </main>
</body>
</html>
"""


def _string_param(params: Any, key: str) -> str | None:
    if params is None:
        return None
    value = params.get(key)
    return value if isinstance(value, str) else None


def _issuers_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    left_key = _resource_origin_key(left)
    right_key = _resource_origin_key(right)
    return left_key is not None and left_key == right_key


def _resource_origin_key(url: str) -> tuple[str, str, int | None] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    port = parsed.port
    if parsed.scheme == "https" and port == 443:
        port = None
    elif parsed.scheme == "http" and port == 80:
        port = None
    return parsed.scheme.lower(), host, port


def _resource_path_key(url: str) -> str:
    path = urlparse(url).path.rstrip("/") or "/"
    mcp = config.http_path().rstrip("/") or "/mcp"
    if path in {"/", mcp}:
        return mcp
    return path


def _audience_values(aud: Any) -> list[str]:
    if aud is None:
        return []
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, (list, tuple)):
        return [str(item) for item in aud]
    return [str(aud)]


def _audience_as_str(aud: Any) -> str | None:
    values = _audience_values(aud)
    return values[0] if values else None


def _scopes_from_claim(scope: Any) -> list[str]:
    if isinstance(scope, str) and scope.strip():
        return scope.split()
    if isinstance(scope, list):
        return [str(item) for item in scope]
    return list(SCOPES_SUPPORTED)


