from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import jwt
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

import db
import server
from gb_mcp import config
from gb_mcp import http as gb_http
from gb_mcp.http import (
    create_http_app,
    mcp_resource_url,
    public_base_url,
    require_http_credentials,
)

from rom_builder import make_rom

TOKEN = "test-bearer-token"
PROTOCOL_VERSION = "2025-11-25"


def _mcp_headers(token: str | None = TOKEN, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers


def _jsonrpc_from_response(response) -> dict[str, Any] | None:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
        return None
    if not response.content:
        return None
    return response.json()


@pytest.fixture
def http_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GB_MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.delenv("GB_MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("GB_MCP_JWT_SECRET", raising=False)
    monkeypatch.delenv("GB_MCP_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("GB_MCP_TRANSPORT", raising=False)


@pytest.fixture
def http_client(http_env) -> TestClient:
    app = create_http_app(server.mcp)
    with TestClient(app) as client:
        yield client


def test_mcp_rejects_missing_bearer(http_client: TestClient) -> None:
    response = http_client.post(
        "/mcp",
        headers=_mcp_headers(token=None),
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    www = response.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert "resource_metadata=" in www
    assert "error=" in www
    assert "error_description=" in www
    body = response.json()
    assert body["error"] == "invalid_token"
    assert "result" not in body


def test_mcp_rejects_invalid_bearer(http_client: TestClient) -> None:
    response = http_client.post(
        "/mcp",
        headers=_mcp_headers(token="wrong-token"),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers


def _initialize(client: TestClient, token: str = TOKEN) -> str:
    response = client.post(
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
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    message = _jsonrpc_from_response(response)
    assert message is not None
    assert message.get("result", {}).get("serverInfo", {}).get("name") == "gb-mcp-server"
    session_id = response.headers.get("mcp-session-id")
    assert session_id
    ack = client.post(
        "/mcp",
        headers=_mcp_headers(token=token, session_id=session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert ack.status_code in {200, 202}
    return session_id


def test_initialize_tools_list_and_tool_call_with_bearer(
    http_client: TestClient, isolated_db, roms_dir
) -> None:
    session_id = _initialize(http_client)

    listed = http_client.post(
        "/mcp",
        headers=_mcp_headers(session_id=session_id),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    listed_msg = _jsonrpc_from_response(listed)
    assert listed_msg is not None
    names = [tool["name"] for tool in listed_msg["result"]["tools"]]
    assert set(names) == {"add_rom", "list_games", "boot", "play", "save", "stop"}
    assert "begin_gb_rom_upload" not in names
    assert "send_pyboy_input" not in names

    name = "d" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    (dest / "tetris.gb").write_bytes(make_rom(title=b"TETRIS"))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")

    called = http_client.post(
        "/mcp",
        headers=_mcp_headers(session_id=session_id),
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "list_games",
                "arguments": {},
            },
        },
    )
    assert called.status_code == 200
    called_msg = _jsonrpc_from_response(called)
    assert called_msg is not None
    assert "error" not in called_msg
    content = called_msg["result"]["content"]
    text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
    payload = json.loads(text)
    assert payload["ok"] is False
    assert payload["model_request"]["name"] == "email"


def test_public_url_unset_does_not_crash(http_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GB_MCP_PUBLIC_URL", raising=False)
    assert config.public_url() is None
    app = create_http_app(server.mcp)
    with TestClient(app) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={"Host": "127.0.0.1:8080"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "http://127.0.0.1:8080/mcp"
    assert body["authorization_servers"] == ["http://127.0.0.1:8080"]
    assert body["bearer_methods_supported"] == ["header"]
    assert body["scopes_supported"] == ["mcp"]
    assert body["resource_name"] == "gb-mcp-server"


def test_well_known_uses_public_url_when_set(http_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GB_MCP_PUBLIC_URL", "https://gb-mcp-server.com")
    app = create_http_app(server.mcp)
    with TestClient(app) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={"Host": "ignored.example"},
        )
        mcp_denied = client.post(
            "/mcp",
            headers=_mcp_headers(token=None),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://gb-mcp-server.com/mcp"
    assert body["authorization_servers"] == ["https://gb-mcp-server.com"]
    assert "https://gb-mcp-server.com/.well-known/oauth-protected-resource" in mcp_denied.headers[
        "www-authenticate"
    ]


def test_protected_resource_metadata_is_served_at_root_and_path_aware(
    http_client: TestClient,
) -> None:
    root = http_client.get("/.well-known/oauth-protected-resource")
    path_aware = http_client.get("/.well-known/oauth-protected-resource/mcp")
    assert root.status_code == 200
    assert path_aware.status_code == 200
    assert root.json() == path_aware.json()
    assert root.json()["authorization_servers"]


def test_authorization_server_metadata_advertises_pkce_and_dcr(
    http_client: TestClient,
) -> None:
    paths = [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
        "/.well-known/openid-configuration",
    ]
    for path in paths:
        response = http_client.get(path)
        assert response.status_code == 200, path
        body = response.json()
        assert body["issuer"]
        assert body["authorization_endpoint"].endswith("/authorize")
        assert body["token_endpoint"].endswith("/token")
        assert body["registration_endpoint"].endswith("/register")
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert body["response_types_supported"] == ["code"]
        assert "authorization_code" in body["grant_types_supported"]
        assert "refresh_token" in body["grant_types_supported"]
        assert body["token_endpoint_auth_methods_supported"] == ["none"]
        assert "authorization_response_iss_parameter_supported" not in body


def test_mcp_options_does_not_require_bearer(http_client: TestClient) -> None:
    response = http_client.options("/mcp")
    assert response.status_code != 401


def test_origin_url_is_an_mcp_alias(http_client: TestClient) -> None:
    """ChatGPT connectors paste the origin; `/` must not 404."""
    response = http_client.post(
        "/",
        headers=_mcp_headers(token=None),
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    assert "resource_metadata=" in response.headers["www-authenticate"]
    assert response.json()["error"] == "invalid_token"


def test_origin_get_is_protected_even_with_browser_accept(http_client: TestClient) -> None:
    mcp = http_client.get(
        "/",
        headers={"Accept": "application/json, text/event-stream"},
    )
    browser = http_client.get(
        "/",
        headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
    )
    assert mcp.status_code == 401
    assert browser.status_code == 401
    assert "www-authenticate" in mcp.headers
    assert "www-authenticate" in browser.headers


def test_default_cors_allows_chatgpt_origin(http_client: TestClient) -> None:
    response = http_client.options(
        "/mcp",
        headers={
            "Origin": "https://chatgpt.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,accept,mcp-session-id",
        },
    )
    assert response.status_code in {200, 204}
    assert response.headers.get("access-control-allow-origin") == "*"
    assert response.headers.get("access-control-allow-credentials") in {None, "false"}
    root = http_client.options(
        "/",
        headers={
            "Origin": "https://chatgpt.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert root.headers.get("access-control-allow-origin") == "*"


def test_jwt_bearer_is_accepted(http_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GB_MCP_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("GB_MCP_JWT_SECRET", "jwt-test-secret-32-bytes-minimum!")
    token = jwt.encode({"sub": "tests"}, "jwt-test-secret-32-bytes-minimum!", algorithm="HS256")
    app = create_http_app(server.mcp)
    with TestClient(app) as client:
        session_id = _initialize(client, token=token)
        listed = client.post(
            "/mcp",
            headers=_mcp_headers(token=token, session_id=session_id),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert listed.status_code == 200
    assert _jsonrpc_from_response(listed) is not None


def test_cors_does_not_reflect_arbitrary_origin_with_credentials(
    http_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GB_MCP_CORS_ORIGINS", "http://localhost:6274")
    app = create_http_app(server.mcp)
    with TestClient(app) as client:
        allowed = client.options(
            "/mcp",
            headers={
                "Origin": "http://localhost:6274",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,accept",
            },
        )
        rejected = client.options(
            "/mcp",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        get_allowed = client.get(
            "/.well-known/oauth-protected-resource",
            headers={"Origin": "http://localhost:6274"},
        )
        register_allowed = client.options(
            "/register",
            headers={
                "Origin": "http://localhost:6274",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        authorize_allowed = client.options(
            "/authorize",
            headers={
                "Origin": "http://localhost:6274",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:6274"
    assert allowed.headers.get("access-control-allow-credentials") in {None, "false"}
    assert rejected.headers.get("access-control-allow-origin") != "https://evil.example"
    assert get_allowed.headers.get("access-control-allow-credentials") in {None, "false"}
    assert register_allowed.headers.get("access-control-allow-origin") == "http://localhost:6274"
    assert authorize_allowed.headers.get("access-control-allow-origin") == "http://localhost:6274"


def test_main_defaults_to_stdio(http_env, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: dict[str, Any] = {}
    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: ran.update(kwargs))
    server.main([])
    assert ran == {"transport": "stdio"}


def test_main_http_flag_starts_http(http_env, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setattr(server, "run_http", lambda mcp_server: called.append(mcp_server))
    server.main(["--http"])
    assert called == [server.mcp]


def test_http_mode_requires_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GB_MCP_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("GB_MCP_JWT_SECRET", raising=False)
    with pytest.raises(SystemExit):
        require_http_credentials()


def test_public_base_url_prefers_env_over_host(http_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GB_MCP_PUBLIC_URL", "https://gb-mcp-server.com/")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"host", b"other.example")],
            "scheme": "http",
            "server": ("127.0.0.1", 8080),
        }
    )
    assert public_base_url(request) == "https://gb-mcp-server.com"
    assert mcp_resource_url(request) == "https://gb-mcp-server.com/mcp"


def test_public_base_url_follows_www_alias_of_configured_origin(
    http_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GB_MCP_PUBLIC_URL", "https://gb-mcp-server.com")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (b"host", b"www.gb-mcp-server.com"),
                (b"x-forwarded-proto", b"https"),
            ],
            "scheme": "http",
            "server": ("127.0.0.1", 8080),
        }
    )
    assert public_base_url(request) == "https://www.gb-mcp-server.com"
    assert mcp_resource_url(request) == "https://www.gb-mcp-server.com/mcp"


def test_well_known_on_www_host_matches_www_resource(
    http_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GB_MCP_PUBLIC_URL", "https://gb-mcp-server.com")
    app = create_http_app(server.mcp)
    with TestClient(app) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={"Host": "www.gb-mcp-server.com", "X-Forwarded-Proto": "https"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://www.gb-mcp-server.com/mcp"
    assert body["authorization_servers"] == ["https://www.gb-mcp-server.com"]


@pytest.fixture
def fake_http_docker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gb_http, "_docker_available", lambda: None)
    monkeypatch.setattr(gb_http, "_ensure_image", lambda: None)
    monkeypatch.setattr(gb_http, "_create_isolated_container", lambda: "cid")
    monkeypatch.setattr(gb_http, "_destroy_container", lambda _cid: None)
    monkeypatch.setattr(
        gb_http,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": True, "reason": "ok"},
    )
    return monkeypatch


def _rom_dirs(roms_dir: Path) -> list[Path]:
    return [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]


def test_post_roms_rejects_missing_bearer(
    http_client: TestClient, isolated_db, roms_dir: Path, fake_http_docker
) -> None:
    response = http_client.post(
        "/roms",
        headers={"Content-Type": "application/octet-stream"},
        content=make_rom(),
    )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    www = response.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert "resource_metadata=" in www
    assert "error=" in www
    assert "error_description=" in www
    assert response.json()["error"] == "invalid_token"
    assert _rom_dirs(roms_dir) == []


def test_post_roms_rejects_invalid_bearer(
    http_client: TestClient, isolated_db, roms_dir: Path, fake_http_docker
) -> None:
    response = http_client.post(
        "/roms",
        headers={
            "Authorization": "Bearer wrong-token",
            "Content-Type": "application/octet-stream",
        },
        content=make_rom(),
    )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    assert response.json()["error"] == "invalid_token"
    assert _rom_dirs(roms_dir) == []


def test_post_roms_options_does_not_require_bearer(http_client: TestClient) -> None:
    response = http_client.options("/roms")
    assert response.status_code != 401


def test_post_roms_accepts_multipart_file(
    http_client: TestClient, isolated_db, roms_dir: Path, fake_http_docker
) -> None:
    rom = make_rom(title=b"TETRIS")
    response = http_client.post(
        "/roms",
        headers={"Authorization": f"Bearer {TOKEN}"},
        files={"file": ("tetris.gb", rom, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["saved"] is True
    assert body["mapped"] is False
    saved = roms_dir / body["subdirectory"] / "tetris.gb"
    assert saved.read_bytes() == rom


def test_post_roms_persists_with_static_bearer(
    http_client: TestClient, isolated_db, roms_dir: Path, fake_http_docker
) -> None:
    rom = make_rom(title=b"TETRIS")
    response = http_client.post(
        "/roms",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "filename": "tetris.gb",
            "rom_base64": base64.b64encode(rom).decode(),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["saved"] is True
    assert body["mapped"] is False
    assert "email" not in body
    name = body["subdirectory"]
    assert len(name) == db.SUBDIRECTORY_NAME_LENGTH
    saved = roms_dir / name / "tetris.gb"
    assert saved.is_file()
    assert saved.read_bytes() == rom
    assert body["validation"]["valid"] is True
    with db.session_scope() as session:
        assert db.list_subdirectories_for_email(session, "owner@example.com") == []


def test_post_roms_maps_operator_jwt_email(
    http_env, monkeypatch: pytest.MonkeyPatch, isolated_db, roms_dir: Path, fake_http_docker
) -> None:
    secret = "jwt-test-secret-32-bytes-minimum!"
    monkeypatch.setenv("GB_MCP_JWT_SECRET", secret)
    token = jwt.encode(
        {"email": "owner@example.com"}, secret, algorithm="HS256"
    )
    rom = make_rom(title=b"TETRIS")
    app = create_http_app(server.mcp)
    with TestClient(app) as client:
        response = client.post(
            "/roms",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            content=rom,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["saved"] is True
    assert body["mapped"] is True
    assert body["email"] == "owner@example.com"
    name = body["subdirectory"]
    assert len(name) == db.SUBDIRECTORY_NAME_LENGTH
    files = [p for p in (roms_dir / name).iterdir() if p.is_file() and not p.name.startswith(".")]
    assert len(files) == 1
    assert files[0].read_bytes() == rom
    with db.session_scope() as session:
        listed = db.list_subdirectories_for_email(session, "owner@example.com")
        assert [row.name for row in listed] == [name]
