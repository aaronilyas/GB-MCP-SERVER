from __future__ import annotations

import inspect
from pathlib import Path

import db
import server

from rom_builder import make_rom

_TOOL_NAMES = (
    "submit_gb_rom",
    "map_subdirectory_to_email",
    "list_subdirectories_for_email",
    "load_subdirectory_rom",
    "send_pyboy_input",
    "stop_pyboy",
)

_OPS_SUBSTRINGS = (
    "GB_MCP_BEARER_TOKEN",
    "GB_MCP_JWT_SECRET",
    "docker.sock",
    "Authorization",
    "Bearer",
    "password",
    "JWT",
)


def _mapped_rom(roms_dir: Path, *, email: str = "owner@example.com", name: str | None = None) -> str:
    name = name or ("a" * db.SUBDIRECTORY_NAME_LENGTH)
    dest = roms_dir / name
    dest.mkdir()
    (dest / "tetris.gb").write_bytes(make_rom(title=b"TETRIS"))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, email)
    return name


def test_owned_roms_resource(isolated_db, roms_dir: Path) -> None:
    name = _mapped_rom(roms_dir)
    result = server.owned_roms_resource("Owner@Example.com")
    assert result["email"] == "owner@example.com"
    assert result["count"] == 1
    assert result["subdirectories"][0]["subdirectory"] == name
    assert result["subdirectories"][0]["games"][0]["title"] == "TETRIS"


def test_rom_header_resource_requires_ownership(isolated_db, roms_dir: Path) -> None:
    name = _mapped_rom(roms_dir)
    denied = server.rom_header_resource("other@example.com", name)
    assert "error" in denied
    assert "not mapped" in denied["error"]

    header = server.rom_header_resource("owner@example.com", name)
    assert header["email"] == "owner@example.com"
    assert header["subdirectory"] == name
    assert header["games"][0]["title"] == "TETRIS"
    assert header["games"][0]["platform"] == "Game Boy"


def test_session_status_resource(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    idle = server.session_status_resource("owner@example.com")
    assert idle == {"email": "owner@example.com", "running": False}

    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name)
    live = server.session_status_resource("Owner@Example.com")
    assert live["running"] is True
    assert live["email"] == "owner@example.com"
    assert live["subdirectory"] == name
    assert live["rom"] == "tetris.gb"


def test_usage_resource_is_static_markdown_howto() -> None:
    body = server.usage_resource()
    assert isinstance(body, str)
    assert body.strip()
    for name in _TOOL_NAMES:
        assert name in body

    assert inspect.signature(server.usage_resource).parameters == {}
    resources = {str(item.uri): item for item in server.mcp._resource_manager.list_resources()}
    usage = resources["gb://usage"]
    assert "{" not in str(usage.uri)
    assert usage.mime_type == "text/markdown"
    templates = [item.uri_template for item in server.mcp._resource_manager.list_templates()]
    assert "gb://usage" not in templates


def test_usage_resource_body_is_stable() -> None:
    first = server.usage_resource()
    second = server.usage_resource()
    assert first == second
    assert first == server._USAGE_GUIDE


def test_usage_resource_omits_ops_secrets() -> None:
    body = server.usage_resource()
    for needle in _OPS_SUBSTRINGS:
        assert needle not in body, needle
