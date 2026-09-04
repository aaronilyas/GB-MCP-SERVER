"""Streamable HTTP front-end: bearer auth, CORS, and public-resource metadata.

Stdio remains the default transport. This module is imported by `server.py` so
HTTP mode can wrap the same MCP tools and resources without baking a hostname
into the process.
"""

from __future__ import annotations

import secrets
from typing import Any

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

_WELL_KNOWN_PATH = "/.well-known/oauth-protected-resource"
_JWT_ALGORITHMS = ["HS256"]
_attached_servers: set[int] = set()


def _fallback_origin() -> str:
    return f"http://127.0.0.1:{config.http_port()}"


def public_base_url(request: Request | None = None) -> str:
    """Origin used for absolute links and RFC 9728 `resource`.

    Prefer `GB_MCP_PUBLIC_URL`. If unset, use the request Host (and
    X-Forwarded-Proto when present). Last resort is loopback with the
    configured port. Never raises because a domain is missing.
    """
    configured = config.public_url()
    if configured:
        return configured
    if request is not None:
        host = (request.headers.get("host") or "").strip()
        if host:
            forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
            scheme = forwarded or request.url.scheme or "http"
            return f"{scheme}://{host}"
    return _fallback_origin()


def mcp_resource_url(request: Request | None = None) -> str:
    """Protected resource identifier: public origin + MCP path."""
    return f"{public_base_url(request)}{config.http_path()}"


def resource_metadata_url(request: Request | None = None) -> str:
    return f"{public_base_url(request)}{_WELL_KNOWN_PATH}"


def protected_resource_payload(request: Request | None = None) -> dict[str, Any]:
    """RFC 9728 metadata. `resource` follows GB_MCP_PUBLIC_URL when set."""
    return {
        "resource": mcp_resource_url(request),
        "bearer_methods_supported": ["header"],
        "resource_name": "gb-mcp-server",
    }


async def oauth_protected_resource(request: Request) -> Response:
    return JSONResponse(
        protected_resource_payload(request),
        headers={"Cache-Control": "public, max-age=3600"},
    )


def authenticate_bearer(authorization: str | None) -> bool:
    """Return True if the Authorization value is an accepted bearer token."""
    if not authorization:
        return False
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return False
    token = credential.strip()
    shared = config.bearer_token()
    if shared is not None and _constant_time_equals(token, shared):
        return True
    secret = config.jwt_secret()
    if secret is not None:
        try:
            jwt.decode(token, secret, algorithms=_JWT_ALGORITHMS)
            return True
        except PyJWTError:
            return False
    return False


def _constant_time_equals(given: str, expected: str) -> bool:
    given_bytes = given.encode("utf-8")
    expected_bytes = expected.encode("utf-8")
    if len(given_bytes) != len(expected_bytes):
        return False
    return secrets.compare_digest(given_bytes, expected_bytes)


def www_authenticate_value(request: Request) -> str:
    metadata = resource_metadata_url(request)
    return (
        'Bearer realm="gb-mcp-server", error="invalid_token", '
        'error_description="Authentication required", '
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
        if authenticate_bearer(headers.get("authorization")):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        response = unauthorized_response(request)
        await response(scope, receive, send)


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
    """Register unauthenticated HTTP routes (well-known metadata). Idempotent."""
    marker = id(mcp_server)
    if marker in _attached_servers:
        return
    _attached_servers.add(marker)
    mcp_server.custom_route(_WELL_KNOWN_PATH, methods=["GET"])(oauth_protected_resource)


def create_http_app(mcp_server: MCPServer) -> Starlette:
    """Starlette app serving Streamable HTTP MCP with bearer auth.

    `GB_MCP_PUBLIC_URL` is not required. DNS-rebinding Host allowlists are
    off because the public hostname is tunnel configuration, not app config.
    """
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
