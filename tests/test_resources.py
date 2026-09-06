from __future__ import annotations

from pathlib import Path

import db
import server
from gb_mcp.contract import HOW_TO_PLAY
from gb_mcp.http import oauth_token_claims

from rom_builder import make_rom

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


def test_resource_uris() -> None:
    resources = {str(item.uri): item for item in server.mcp._resource_manager.list_resources()}
    assert set(resources) == {"gb://how-to-play", "gb://screen", "gb://session"}
    assert "gb://usage" not in resources
    templates = [item.uri_template for item in server.mcp._resource_manager.list_templates()]
    assert templates == []
    assert resources["gb://how-to-play"].mime_type == "text/markdown"


def test_how_to_play_matches_instructions() -> None:
    body = server.how_to_play_resource()
    assert body == HOW_TO_PLAY
    assert body == server.mcp.instructions
    for needle in _OPS_SUBSTRINGS:
        assert needle not in body, needle
    for old in (
        "blake2s",
        "battle_likely",
        "begin_gb_rom_upload",
        "ping_pyboy",
        "bearer",
        "send_pyboy_input",
    ):
        assert old not in body


def test_session_resource_idle_and_live(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    with oauth_token_claims({"email": "owner@example.com"}):
        idle = server.session_resource()
        assert idle["stopped"] is True or idle["ok"] is False
        assert "email" not in idle
        _mapped_rom(roms_dir)
        server.boot(title="TETRIS")
        live = server.session_resource()
    assert live["ok"] is True
    assert live["stopped"] is False
    assert "email" not in live
    assert "subdirectory" not in live
    assert "rom_path" not in live


def test_screen_resource_returns_png(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    _mapped_rom(roms_dir)
    with oauth_token_claims({"email": "owner@example.com"}):
        server.boot(title="TETRIS")
        shot = server.screen_resource()
    assert getattr(shot, "data", None) or isinstance(shot, dict)
    if not isinstance(shot, dict):
        assert shot.data.startswith(b"\x89PNG")
