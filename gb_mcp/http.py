"""Streamable HTTP front-end: dual auth, CORS, OAuth, and public metadata.

Stdio remains the default transport. This module is imported by `server.py` so
HTTP mode can wrap the same MCP tools and resources without baking a hostname
into the process. `/mcp` accepts a static bearer / operator JWT or an access
token from the in-process OAuth authorization server.
"""

from __future__ import annotations

import contextvars
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from urllib.parse import urlparse

import jwt
from jwt.exceptions import PyJWTError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from gb_mcp import config
from gb_mcp.oauth import (
    MCP_SCOPE,
    authorization_server_payload,
    decode_access_token_claims,
    handle_authorize,
    handle_register,
    handle_token,
    oauth_claims_match_request,
    protected_resource_fields,
    reset_oauth_state,
)

_WELL_KNOWN_PRM = "/.well-known/oauth-protected-resource"
_WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"
_WELL_KNOWN_OIDC = "/.well-known/openid-configuration"
_JWT_ALGORITHMS = ["HS256"]
_attached_servers: set[int] = set()
# Application identity from the current access token. Not transport auth.
_token_claims: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "gb_mcp_token_claims", default=None
)


def _fallback_origin() -> str:
    return f"http://127.0.0.1:{config.http_port()}"


def _origin_from_request(request: Request) -> str | None:
    host = (request.headers.get("host") or "").strip()
    if not host:
        return None
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = forwarded or request.url.scheme or "http"
    return f"{scheme}://{host}"


def _hostname_key(host: str) -> str:
    value = host.lower()
    return value[4:] if value.startswith("www.") else value


def _effective_port(parsed) -> int | None:
    port = parsed.port
    if parsed.scheme == "https" and port == 443:
        return None
    if parsed.scheme == "http" and port == 80:
        return None
    return port


def _same_public_site(origin: str, configured: str) -> bool:
    """True when origin is the configured public site, including a www alias."""
    left = urlparse(origin)
    right = urlparse(configured)
    if left.scheme.lower() != right.scheme.lower():
        return False
    left_host = _hostname_key(left.hostname or "")
    right_host = _hostname_key(right.hostname or "")
    if not left_host or left_host != right_host:
        return False
    return _effective_port(left) == _effective_port(right)


def public_base_url(request: Request | None = None) -> str:
    """Origin used for absolute links and RFC 9728 `resource`.

    Prefer `GB_MCP_PUBLIC_URL`. If the request Host is the same site as that
    origin (including a ``www.`` alias), use the request origin so ChatGPT
    connectors pasted at ``https://www.…`` see matching issuer/resource URLs.
    If unset, use the request Host (and X-Forwarded-Proto when present). Last
    resort is loopback with the configured port. Never raises because a domain
    is missing.
    """
    configured = config.public_url()
    request_origin = _origin_from_request(request) if request is not None else None
    if configured:
        if request_origin and _same_public_site(request_origin, configured):
            return request_origin.rstrip("/")
        return configured
    if request_origin:
        return request_origin.rstrip("/")
    return _fallback_origin()


def mcp_resource_url(request: Request | None = None) -> str:
    """Protected resource identifier: public origin + MCP path."""
    return f"{public_base_url(request)}{config.http_path()}"


def resource_metadata_url(request: Request | None = None) -> str:
    return f"{public_base_url(request)}{_WELL_KNOWN_PRM}"


def path_aware_well_known(kind: str) -> str:
    """RFC 9728 / RFC 8414 path insertion: `/.well-known/{kind}{mcp_path}`."""
    return f"/.well-known/{kind}{config.http_path()}"


def protected_resource_payload(request: Request | None = None) -> dict[str, Any]:
    """RFC 9728 metadata. `resource` and `authorization_servers` follow the public origin."""
    return protected_resource_fields(mcp_resource_url(request), public_base_url(request))


async def oauth_protected_resource(request: Request) -> Response:
    return JSONResponse(
        protected_resource_payload(request),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def oauth_authorization_server(request: Request) -> Response:
    return JSONResponse(
        authorization_server_payload(public_base_url(request)),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def authorize_endpoint(request: Request) -> Response:
    return await handle_authorize(
        request,
        issuer=public_base_url(request),
        resource=mcp_resource_url(request),
    )


async def token_endpoint(request: Request) -> Response:
    return await handle_token(request, resource=mcp_resource_url(request))


async def register_endpoint(request: Request) -> Response:
    return await handle_register(request)


def authenticate_bearer(authorization: str | None, request: Request | None = None) -> bool:
    """Return True if the Authorization value is an accepted bearer token.

    Accepts the shared ``GB_MCP_BEARER_TOKEN``, an operator HS256 JWT signed
    with ``GB_MCP_JWT_SECRET``, or an access token issued by this process's
    authorization server. OAuth tokens must match this request's issuer and
    resource (``aud``). Email claims are not used here; they are application
    identity for tools, not transport authentication.
    """
    return bearer_token_claims(authorization, request) is not False


def bearer_token_claims(
    authorization: str | None, request: Request | None = None
) -> dict[str, Any] | None | Literal[False]:
    """Validate the bearer and return JWT claims when the credential is a JWT.

    ``False`` means unauthenticated. ``None`` means authenticated via the
    shared static bearer (no OAuth identity). A ``dict`` is the verified JWT
    payload. Callers must not treat ``email`` / ``sub`` as transport auth.
    """
    if not authorization:
        return False
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return False
    token = credential.strip()
    shared = config.bearer_token()
    if shared is not None and _constant_time_equals(token, shared):
        return None
    return _jwt_claims(token, request)


def current_token_claims() -> dict[str, Any] | None:
    """JWT claims for the in-flight HTTP request, if any."""
    return _token_claims.get()


def current_oauth_identity() -> str | None:
    """Application identity from the current access token ``email`` or ``sub``.

    Prefers ``email`` when present. Does not authenticate the request; bearer
    / OAuth already ran in this module.
    """
    claims = _token_claims.get()
    if not claims:
        return None
    for key in ("email", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@contextmanager
def oauth_token_claims(claims: dict[str, Any] | None) -> Iterator[None]:
    """Bind token claims for this task (tests and HTTP middleware)."""
    token = _token_claims.set(claims)
    try:
        yield
    finally:
        _token_claims.reset(token)


def _jwt_claims(token: str, request: Request | None) -> dict[str, Any] | Literal[False]:
    operator_secret = config.jwt_secret()
    if operator_secret is not None:
        try:
            claims = jwt.decode(
                token,
                operator_secret,
                algorithms=_JWT_ALGORITHMS,
                options={"verify_aud": False},
            )
        except PyJWTError:
            claims = None
        if isinstance(claims, dict):
            if "iss" in claims or "aud" in claims:
                return claims if _oauth_jwt_ok(claims, request) else False
            return claims

    claims = decode_access_token_claims(token)
    if claims is None:
        return False
    if "iss" in claims or "aud" in claims:
        return claims if _oauth_jwt_ok(claims, request) else False
    return claims if operator_secret is not None else False


def _oauth_jwt_ok(claims: dict[str, Any], request: Request | None) -> bool:
    if request is None:
        return False
    return oauth_claims_match_request(
        claims,
        issuer=public_base_url(request),
        resource=mcp_resource_url(request),
    )


def _constant_time_equals(given: str, expected: str) -> bool:
    given_bytes = given.encode("utf-8")
    expected_bytes = expected.encode("utf-8")
    if len(given_bytes) != len(expected_bytes):
        return False
    return secrets.compare_digest(given_bytes, expected_bytes)


def www_authenticate_value(request: Request) -> str:
    metadata = resource_metadata_url(request)
    return (
        'Bearer realm="gb-mcp-server", '
        'error="invalid_token", '
        'error_description="Authentication required", '
        f'scope="{MCP_SCOPE}", '
        f'resource_metadata="{metadata}"'
    )


def unauthorized_response(request: Request) -> JSONResponse:
    body = {
        "error": "invalid_token",
        "error_description": "Authentication required",
    }
    return JSONResponse(
        body,
        status_code=401,
        headers={"WWW-Authenticate": www_authenticate_value(request)},
    )


_MCP_ROOT_METHODS = frozenset({"GET", "POST", "DELETE", "OPTIONS"})


class RootMcpAliasMiddleware:
    """Serve MCP at ``/`` as well as ``GB_MCP_PATH`` (default ``/mcp``).

    ChatGPT custom connectors probe the exact URL the user pastes. Users often
    paste the origin (``https://www.example.com``) rather than ``/mcp``. A 404
    at ``/`` is reported as "Connection failed" before OAuth can start.
    """

    def __init__(self, app: ASGIApp, mcp_path: str) -> None:
        self.app = app
        self.mcp_path = mcp_path.rstrip("/") or "/mcp"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self.mcp_path in {"/", ""}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        method = scope.get("method") or "GET"
        if path not in {"/", ""} or method not in _MCP_ROOT_METHODS:
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        scope["path"] = self.mcp_path
        raw = scope.get("raw_path")
        if isinstance(raw, (bytes, bytearray)):
            query = bytes(raw).split(b"?", 1)
            suffix = b"?" + query[1] if len(query) == 2 else b""
            scope["raw_path"] = self.mcp_path.encode("ascii") + suffix
        await self.app(scope, receive, send)


class BearerAuthMiddleware:
    """Require a bearer token on the MCP HTTP path. Does not buffer SSE bodies."""

    def __init__(self, app: ASGIApp, mcp_path: str) -> None:
        self.app = app
        self.mcp_path = mcp_path.rstrip("/") or "/mcp"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = (scope.get("path") or "").rstrip("/") or "/"
        if path != self.mcp_path:
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request = Request(scope, receive)
        claims = bearer_token_claims(headers.get("authorization"), request)
        if claims is False:
            response = unauthorized_response(request)
            await response(scope, receive, send)
            return

        token = _token_claims.set(claims if isinstance(claims, dict) else None)
        try:
            await self.app(scope, receive, send)
        finally:
            _token_claims.reset(token)


def _cors_middleware() -> Middleware | None:
    origins = config.cors_origins()
    if not origins:
        return None
    return Middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept",
            "mcp-session-id",
            "mcp-protocol-version",
            "Last-Event-ID",
        ],
        expose_headers=["mcp-session-id", "WWW-Authenticate", "Content-Type"],
    )


def attach_public_routes(mcp_server: MCPServer) -> None:
    """Register unauthenticated discovery and OAuth routes. Idempotent."""
    marker = id(mcp_server)
    if marker in _attached_servers:
        return
    _attached_servers.add(marker)
    prm_path = path_aware_well_known("oauth-protected-resource")
    as_path = path_aware_well_known("oauth-authorization-server")
    mcp_server.custom_route(_WELL_KNOWN_PRM, methods=["GET"])(oauth_protected_resource)
    if prm_path != _WELL_KNOWN_PRM:
        mcp_server.custom_route(prm_path, methods=["GET"])(oauth_protected_resource)
    mcp_server.custom_route(_WELL_KNOWN_AS, methods=["GET"])(oauth_authorization_server)
    if as_path != _WELL_KNOWN_AS:
        mcp_server.custom_route(as_path, methods=["GET"])(oauth_authorization_server)
    mcp_server.custom_route(_WELL_KNOWN_OIDC, methods=["GET"])(oauth_authorization_server)
    mcp_server.custom_route("/authorize", methods=["GET", "POST"])(authorize_endpoint)
    mcp_server.custom_route("/token", methods=["POST"])(token_endpoint)
    mcp_server.custom_route("/register", methods=["POST"])(register_endpoint)


def create_http_app(mcp_server: MCPServer) -> Starlette:
    """Starlette app serving Streamable HTTP MCP with bearer auth.

    `GB_MCP_PUBLIC_URL` is not required. DNS-rebinding Host allowlists are
    off because the public hostname is tunnel configuration, not app config.
    """
    reset_oauth_state()
    attach_public_routes(mcp_server)
    path = config.http_path()
    app = mcp_server.streamable_http_app(
        streamable_http_path=path,
        host=config.http_host(),
        json_response=False,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    app.add_middleware(BearerAuthMiddleware, mcp_path=path)
    cors = _cors_middleware()
    if cors is not None:
        app.add_middleware(
            cors.cls,
            **cors.kwargs,
        )
    # Outermost: ChatGPT probes the pasted origin (``/``) before ``/mcp``.
    app.add_middleware(RootMcpAliasMiddleware, mcp_path=path)
    return app


def require_http_credentials() -> None:
    if config.bearer_token() is None and config.jwt_secret() is None:
        raise SystemExit(
            "HTTP mode requires GB_MCP_BEARER_TOKEN or GB_MCP_JWT_SECRET so "
            "the open internet cannot call tools."
        )


def run_http(mcp_server: MCPServer) -> None:
    """Bind Streamable HTTP and serve until interrupted."""
    import uvicorn

    require_http_credentials()
    app = create_http_app(mcp_server)
    uvicorn.run(
        app,
        host=config.http_host(),
        port=config.http_port(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=75,
        log_level="info",
        # h11 + no gzip: SSE `text/event-stream` is flushed as events arrive.
        http="h11",
    )
