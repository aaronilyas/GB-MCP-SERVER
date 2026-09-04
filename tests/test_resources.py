from __future__ import annotations

from pathlib import Path

import db
import server

from rom_builder import make_rom


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
