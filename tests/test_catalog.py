from __future__ import annotations

from pathlib import Path

import db
from gb_mcp.tools.catalog import list_games

from rom_builder import make_rom

_GAME_KEYS = {"title", "id", "playable"}


def _write_mapped_rom(
    roms_dir: Path,
    *,
    email: str,
    name: str,
    title: bytes,
    filename: str = "game.gb",
    data: bytes | None = None,
) -> str:
    dest = roms_dir / name
    dest.mkdir()
    (dest / filename).write_bytes(data if data is not None else make_rom(title=title))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, email)
    return name


def _assert_game_shape(game: dict) -> None:
    assert set(game) == _GAME_KEYS
    assert isinstance(game["id"], str)
    assert len(game["id"]) == db.SUBDIRECTORY_NAME_LENGTH
    assert all(c in "0123456789abcdef" for c in game["id"])
    assert game["title"] is None or isinstance(game["title"], str)
    assert game["playable"] in (True, False)


def test_list_games_two_mapped_roms(isolated_db, roms_dir: Path) -> None:
    email = "owner@example.com"
    tetris_id = "a" * db.SUBDIRECTORY_NAME_LENGTH
    red_id = "b" * db.SUBDIRECTORY_NAME_LENGTH
    _write_mapped_rom(roms_dir, email=email, name=tetris_id, title=b"TETRIS", filename="tetris.gb")
    _write_mapped_rom(
        roms_dir, email=email, name=red_id, title=b"POKEMON RED", filename="red.gb"
    )

    result = list_games("Owner@Example.com")
    games = result["games"]
    assert [g["title"] for g in games] == ["TETRIS", "POKEMON RED"]
    assert [g["id"] for g in games] == [tetris_id, red_id]
    assert all(g["playable"] is True for g in games)
    for game in games:
        _assert_game_shape(game)
        assert "path" not in game
        assert "licensee" not in game
        assert "filename" not in game
        assert "platform" not in game
        assert "cartridge_type" not in game


def test_list_games_unknown_email_is_empty(isolated_db) -> None:
    assert list_games("nobody@example.com") == {"games": []}


def test_list_games_truncated_rom_is_unplayable(isolated_db, roms_dir: Path) -> None:
    name = "c" * db.SUBDIRECTORY_NAME_LENGTH
    _write_mapped_rom(
        roms_dir,
        email="owner@example.com",
        name=name,
        title=b"POKEMON RED",
        filename="red.gb",
        data=make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05),
    )

    games = list_games("owner@example.com")["games"]
    assert len(games) == 1
    _assert_game_shape(games[0])
    assert games[0] == {"title": "POKEMON RED", "id": name, "playable": False}
