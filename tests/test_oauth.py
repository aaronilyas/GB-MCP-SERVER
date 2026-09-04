from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from starlette.testclient import TestClient

from gb_mcp.http import create_http_app
from test_http import PROTOCOL_VERSION, TOKEN, _initialize, _jsonrpc_from_response, _mcp_headers

import server

JWT_SECRET = "jwt-test-secret-32-bytes-minimum!"
PUBLIC_ORIGIN = "https://gb.example"
PUBLIC_RESOURCE = f"{PUBLIC_ORIGIN}/mcp"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _consent_token(html: str) -> str:
    match = re.search(r'name="consent_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.fixture
def oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GB_MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("GB_MCP_PUBLIC_URL", PUBLIC_ORIGIN)
    monkeypatch.setenv("GB_MCP_JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("GB_MCP_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("GB_MCP_TRANSPORT", raising=False)


@pytest.fixture
def oauth_client(oauth_env) -> TestClient:
    app = create_http_app(server.mcp)
    with TestClient(app) as client:
        yield client


def _register(
    client: TestClient,
    redirect_uri: str,
    *,
    client_name: str = "gb-mcp-tests",
) -> dict[str, Any]:
    response = client.post(
        "/register",
        json={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "mcp",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["client_id"]
    assert body.get("token_endpoint_auth_method") == "none"
    return body


def _authorize_and_allow(
    client: TestClient,
    *,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    resource: str = PUBLIC_RESOURCE,
    state: str = "state-1",
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "mcp",
        "state": state,
        "resource": resource,
    }
    page = client.get("/authorize", params=query)
    assert page.status_code == 200
    assert "text/html" in page.headers.get("content-type", "")
    assert "GB_MCP_BEARER_TOKEN" not in page.text
    assert 'type="password"' not in page.text.lower()
    allowed = client.post(
        "/authorize",
        data={"consent_token": _consent_token(page.text), "consent": "allow"},
        follow_redirects=False,
    )
    assert allowed.status_code == 302
    location = allowed.headers["location"]
    parsed = urlparse(location)
    returned = parse_qs(parsed.query)
    assert returned.get("state") == [state]
    assert "code" in returned
    assert parsed.netloc == urlparse(redirect_uri).netloc
    assert parsed.path == urlparse(redirect_uri).path
    return returned["code"][0]


def _token(
    client: TestClient,
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    resource: str = PUBLIC_RESOURCE,
) -> dict[str, Any]:
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    return body


def test_dcr_issues_client_id_for_loopback_and_https(oauth_client: TestClient) -> None:
    loopback = _register(oauth_client, "http://127.0.0.1:9999/callback")
    https = _register(
        oauth_client,
        "https://client.example/oauth/callback",
        client_name="hosted-connector",
    )
    assert loopback["client_id"] != https["client_id"]


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://127.0.0.1:1234/callback",
        "http://localhost:6274/oauth/callback",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://claude.ai/api/mcp/auth_callback",
        "https://gemini.google.com/oauth/mcp/callback",
    ],
)
def test_dcr_accepts_loopback_and_https_callbacks(
    oauth_client: TestClient, redirect_uri: str
) -> None:
    body = _register(oauth_client, redirect_uri)
    assert body["client_id"]


def test_dcr_rejects_http_non_loopback(oauth_client: TestClient) -> None:
    response = oauth_client.post(
        "/register",
        json={
            "client_name": "evil",
            "redirect_uris": ["http://evil.example/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_pkce_authorize_token_initialize_and_tools_list(oauth_client: TestClient) -> None:
    redirect_uri = "http://127.0.0.1:9999/callback"
    registered = _register(oauth_client, redirect_uri)
    verifier, challenge = _pkce()
    code = _authorize_and_allow(
        oauth_client,
        client_id=registered["client_id"],
        redirect_uri=redirect_uri,
        challenge=challenge,
    )
    tokens = _token(
        oauth_client,
        client_id=registered["client_id"],
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
    )
    claims = jwt.decode(
        tokens["access_token"],
        JWT_SECRET,
        algorithms=["HS256"],
        audience=PUBLIC_RESOURCE,
    )
    assert claims["iss"] == PUBLIC_ORIGIN
    assert claims["aud"] == PUBLIC_RESOURCE
    assert claims["sub"]
    assert "mcp" in claims["scope"].split()

    session_id = _initialize(oauth_client, token=tokens["access_token"])
    listed = oauth_client.post(
        "/mcp",
        headers=_mcp_headers(token=tokens["access_token"], session_id=session_id),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    message = _jsonrpc_from_response(listed)
    assert message is not None
    names = [tool["name"] for tool in message["result"]["tools"]]
    assert "list_subdirectories_for_email" in names


def test_wrong_code_verifier_fails(oauth_client: TestClient) -> None:
    redirect_uri = "http://127.0.0.1:9999/callback"
    registered = _register(oauth_client, redirect_uri)
    verifier, challenge = _pkce()
    code = _authorize_and_allow(
        oauth_client,
        client_id=registered["client_id"],
        redirect_uri=redirect_uri,
        challenge=challenge,
    )
    wrong_verifier, _ = _pkce()
    assert wrong_verifier != verifier
    response = oauth_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": registered["client_id"],
            "code_verifier": wrong_verifier,
            "resource": PUBLIC_RESOURCE,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_token_with_wrong_aud_is_401(oauth_client: TestClient) -> None:
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": PUBLIC_ORIGIN,
            "aud": "https://evil.example/mcp",
            "exp": now + 3600,
            "sub": "gb-mcp-user",
            "scope": "mcp",
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    response = oauth_client.post(
        "/mcp",
        headers=_mcp_headers(token=token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "gb-mcp-tests", "version": "0.0.1"},
            },
        },
    )
    assert response.status_code == 401


def test_static_bearer_still_initializes(oauth_client: TestClient) -> None:
    session_id = _initialize(oauth_client, token=TOKEN)
    listed = oauth_client.post(
        "/mcp",
        headers=_mcp_headers(token=TOKEN, session_id=session_id),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    assert _jsonrpc_from_response(listed) is not None


def test_oauth_works_when_only_bearer_token_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GB_MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("GB_MCP_PUBLIC_URL", PUBLIC_ORIGIN)
    monkeypatch.delenv("GB_MCP_JWT_SECRET", raising=False)
    monkeypatch.delenv("GB_MCP_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("GB_MCP_TRANSPORT", raising=False)
    app = create_http_app(server.mcp)
    redirect_uri = "https://client.example/cb"
    with TestClient(app) as client:
        registered = _register(client, redirect_uri)
        verifier, challenge = _pkce()
        code = _authorize_and_allow(
            client,
            client_id=registered["client_id"],
            redirect_uri=redirect_uri,
            challenge=challenge,
        )
        tokens = _token(
            client,
            client_id=registered["client_id"],
            code=code,
            redirect_uri=redirect_uri,
            verifier=verifier,
        )
        session_id = _initialize(client, token=tokens["access_token"])
        listed = client.post(
            "/mcp",
            headers=_mcp_headers(token=tokens["access_token"], session_id=session_id),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert listed.status_code == 200


def test_plain_pkce_method_is_rejected(oauth_client: TestClient) -> None:
    redirect_uri = "http://127.0.0.1:9999/callback"
    registered = _register(oauth_client, redirect_uri)
    verifier, _challenge = _pkce()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    plain = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    response = oauth_client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": redirect_uri,
            "code_challenge": plain,
            "code_challenge_method": "plain",
            "resource": PUBLIC_RESOURCE,
        },
        follow_redirects=False,
    )
    assert response.status_code in {302, 400}
    if response.status_code == 302:
        assert "error=" in response.headers["location"]
    else:
        assert response.json()["error"] in {"invalid_request", "unsupported_response_type"}


def test_refresh_token_rotates(oauth_client: TestClient) -> None:
    redirect_uri = "http://127.0.0.1:9999/callback"
    registered = _register(oauth_client, redirect_uri)
    verifier, challenge = _pkce()
    code = _authorize_and_allow(
        oauth_client,
        client_id=registered["client_id"],
        redirect_uri=redirect_uri,
        challenge=challenge,
    )
    tokens = _token(
        oauth_client,
        client_id=registered["client_id"],
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
    )
    refreshed = oauth_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": registered["client_id"],
            "resource": PUBLIC_RESOURCE,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["refresh_token"] != tokens["refresh_token"]
    reused = oauth_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": registered["client_id"],
            "resource": PUBLIC_RESOURCE,
        },
    )
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"


def test_www_and_origin_resource_urls_match_canonical_mcp() -> None:
    from gb_mcp.oauth import same_resource

    canonical = "https://gb.example/mcp"
    assert same_resource(canonical, canonical)
    assert same_resource("https://www.gb.example/mcp", canonical)
    assert same_resource("https://www.gb.example", canonical)
    assert same_resource("https://gb.example", canonical)
    assert not same_resource("https://evil.example/mcp", canonical)
    assert not same_resource("https://gb.example/other", canonical)


def test_authorize_accepts_www_origin_resource_parameter(oauth_client: TestClient) -> None:
    redirect_uri = "https://chatgpt.com/connector_platform_oauth_redirect"
    registered = _register(oauth_client, redirect_uri)
    verifier, challenge = _pkce()
    code = _authorize_and_allow(
        oauth_client,
        client_id=registered["client_id"],
        redirect_uri=redirect_uri,
        challenge=challenge,
        resource="https://www.gb.example",
    )
    tokens = _token(
        oauth_client,
        client_id=registered["client_id"],
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
        resource="https://www.gb.example",
    )
    session_id = _initialize(oauth_client, token=tokens["access_token"])
    listed = oauth_client.post(
        "/mcp",
        headers=_mcp_headers(token=tokens["access_token"], session_id=session_id),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200


def test_authorize_rejects_unregistered_redirect_host(oauth_client: TestClient) -> None:
    registered = _register(oauth_client, "https://client.example/cb")
    verifier, challenge = _pkce()
    response = oauth_client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "https://client.example.evil/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": PUBLIC_RESOURCE,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert "evil" in response.json()["error_description"]
