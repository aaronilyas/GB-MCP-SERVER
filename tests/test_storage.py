from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import db
from gb_mcp.storage.roms import (
    _allocate_subdirectory_name,
    _describe_subdirectory,
    _isoformat,
    _iter_subdirectory_files,
    _persist_validated_rom,
    _rom_in_subdirectory,
    _sanitize_filename,
    _state_path_for_rom,
)

from rom_builder import make_rom


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("poke.gb", "poke.gb"),
        ("game.gbc", "game.gbc"),
        ("../evil.gb", "evil.gb"),
        ("has spaces.gb", "has_spaces.gb"),
        ("noext", "noext.gb"),
        ("", "rom.gb"),
        ("  ", "rom.gb"),
    ],
)
def test_sanitize_filename(given: str, expected: str) -> None:
    assert _sanitize_filename(given) == expected


def test_sanitize_filename_truncates() -> None:
    name = "a" * 200 + ".gb"
    assert len(_sanitize_filename(name)) == 180


def test_isoformat_naive_assumes_utc() -> None:
    value = datetime(2026, 1, 2, 3, 4, 5)
    assert _isoformat(value) == "2026-01-02T03:04:05+00:00"
    assert _isoformat(None) is None


def test_persist_validated_rom_writes_bytes(roms_dir: Path) -> None:
    dest = _persist_validated_rom("abc", "game.gb", b"hello")
    assert dest.read_bytes() == b"hello"
    assert dest.parent == roms_dir / "abc"
    assert dest.name == "game.gb"
    assert not list(dest.parent.glob(".rom-*"))


def test_persist_validated_rom_collision_gets_suffix(roms_dir: Path) -> None:
    first = _persist_validated_rom("abc", "game.gb", b"one")
    second = _persist_validated_rom("abc", "game.gb", b"two")
    assert first.name == "game.gb"
    assert second.name != "game.gb"
    assert second.name.startswith("game-")
    assert second.suffix == ".gb"
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_allocate_subdirectory_name_unique_on_disk_and_db(
    isolated_db, roms_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = db.new_subdirectory_name()
    (roms_dir / taken).mkdir()
    mapped = db.new_subdirectory_name()
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, mapped, "owner@example.com")

    names = [taken, mapped, "0" * db.SUBDIRECTORY_NAME_LENGTH]

    monkeypatch.setattr(db, "new_subdirectory_name", lambda: names.pop(0))
    allocated = _allocate_subdirectory_name()

    assert allocated == "0" * db.SUBDIRECTORY_NAME_LENGTH
    assert allocated not in (taken, mapped)


def test_allocate_fails_when_all_candidates_taken(isolated_db, roms_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "1" * db.SUBDIRECTORY_NAME_LENGTH
    monkeypatch.setattr(db, "new_subdirectory_name", lambda: name)
    (roms_dir / name).mkdir()
    with pytest.raises(RuntimeError, match="failed to allocate"):
        _allocate_subdirectory_name()


def test_iter_subdirectory_files_skips_dotfiles_and_dirs(roms_dir: Path) -> None:
    dest = roms_dir / "sub"
    dest.mkdir()
    (dest / "z.gb").write_bytes(b"z")
    (dest / "a.gb").write_bytes(b"a")
    (dest / ".hidden").write_bytes(b"h")
    (dest / "nested").mkdir()
    files = _iter_subdirectory_files(dest)
    assert [p.name for p in files] == ["a.gb", "z.gb"]


def test_describe_missing_subdirectory(roms_dir: Path) -> None:
    info = _describe_subdirectory("missing", None)
    assert info["exists_on_disk"] is False
    assert info["summary"] == "mapped in the database but missing from disk"


def test_describe_empty_subdirectory(roms_dir: Path) -> None:
    (roms_dir / "empty").mkdir()
    info = _describe_subdirectory("empty", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert info["exists_on_disk"] is True
    assert info["summary"] == "empty subdirectory"
    assert info["files"] == []
    assert info["games"] == []
    assert info["created_at"] == "2026-01-01T00:00:00+00:00"


def test_describe_rom_and_other_files(roms_dir: Path) -> None:
    dest = roms_dir / "games"
    dest.mkdir()
    (dest / "tetris.gb").write_bytes(make_rom(title=b"TETRIS", cartridge_type=0x03))
    (dest / "notes.txt").write_text("hi")
    (dest / "truncated.gb").write_bytes(b"nope")

    info = _describe_subdirectory("games", None)
    assert info["exists_on_disk"] is True
    assert len(info["files"]) == 3
    assert len(info["games"]) == 1
    assert info["games"][0]["title"] == "TETRIS"
    assert info["games"][0]["has_battery"] is True
    assert info["games"][0]["playable"] is True
    assert "TETRIS" in info["summary"]

    kinds = {entry["filename"]: entry.get("kind") for entry in info["files"]}
    assert kinds["notes.txt"] == "other"
    truncated = next(e for e in info["files"] if e["filename"] == "truncated.gb")
    assert "error" in truncated


def test_state_path_for_rom(tmp_path: Path) -> None:
    rom = tmp_path / "tetris.gb"
    assert _state_path_for_rom(rom) == tmp_path / "tetris.gb.state"


def test_rom_in_subdirectory_missing(roms_dir: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        _rom_in_subdirectory("missing")


def test_rom_in_subdirectory_empty(roms_dir: Path) -> None:
    (roms_dir / "empty").mkdir()
    with pytest.raises(FileNotFoundError, match="no Game Boy ROM"):
        _rom_in_subdirectory("empty")


def test_rom_in_subdirectory_skips_invalid_and_picks_first_valid(roms_dir: Path) -> None:
    dest = roms_dir / "games"
    dest.mkdir()
    (dest / "notes.txt").write_text("hi")
    (dest / "truncated.gb").write_bytes(b"nope")
    (dest / "alpha.gb").write_bytes(make_rom(title=b"ALPHA"))
    (dest / "zeta.gb").write_bytes(make_rom(title=b"ZETA"))
    assert _rom_in_subdirectory("games").name == "alpha.gb"


def test_rom_in_subdirectory_rejects_truncated_pokemon_header(roms_dir: Path) -> None:
    dest = roms_dir / "games"
    dest.mkdir()
    (dest / "red.gb").write_bytes(
        make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    )
    with pytest.raises(ValueError, match="truncated") as excinfo:
        _rom_in_subdirectory("games")
    assert "1024" in str(excinfo.value)
    assert "1048576" in str(excinfo.value)


def test_describe_truncated_rom_flags_unplayable(roms_dir: Path) -> None:
    dest = roms_dir / "games"
    dest.mkdir()
    (dest / "red.gb").write_bytes(
        make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    )
    info = _describe_subdirectory("games", None)
    assert len(info["games"]) == 1
    game = info["games"][0]
    assert game["title"] == "POKEMON RED"
    assert game["playable"] is False
    assert "1024" in game["unplayable_reason"]
    assert "1048576" in game["unplayable_reason"]
    red = next(e for e in info["files"] if e["filename"] == "red.gb")
    assert red["playable"] is False
