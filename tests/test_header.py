from __future__ import annotations

from pathlib import Path

import pytest

from gb_mcp.gb.header import (
    _decode_header_text,
    _read_rom_identity,
    assert_rom_playable,
    inspect_rom_playable,
)

from rom_builder import make_rom


def _write_rom(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_decode_header_text_strips_nuls_and_non_ascii() -> None:
    assert _decode_header_text(b"POKEMON\x00RED") == "POKEMON"
    assert _decode_header_text(b"AB\x01C") == "ABC"


def test_read_rom_identity_original_game_boy(tmp_path: Path) -> None:
    path = _write_rom(
        tmp_path / "game.gb",
        make_rom(title=b"TETRIS", cartridge_type=0x00, destination=0x00),
    )
    identity = _read_rom_identity(path)
    assert identity["kind"] == "rom"
    assert identity["title"] == "TETRIS"
    assert identity["platform"] == "Game Boy"
    assert identity["cgb"] is False
    assert identity["cgb_only"] is False
    assert identity["cartridge_type"] == "ROM only"
    assert identity["has_battery"] is False
    assert identity["destination"] == "Japan"
    assert identity["manufacturer_code"] is None
    assert identity["playable"] is True
    assert "error" not in identity
    assert "unplayable_reason" not in identity


def test_read_rom_identity_cgb_compatible(tmp_path: Path) -> None:
    path = _write_rom(
        tmp_path / "color.gbc",
        make_rom(
            title=b"ZELDA" + b"\x00" * 6 + b"AZL",
            cgb_flag=0x80,
            cartridge_type=0x1B,
            ram_size_code=0x03,
            sgb_flag=0x03,
        ),
    )
    identity = _read_rom_identity(path)
    assert identity["platform"] == "Game Boy Color (GB compatible)"
    assert identity["cgb"] is True
    assert identity["cgb_only"] is False
    assert identity["sgb"] is True
    assert identity["cartridge_type"] == "MBC5+RAM+BATTERY"
    assert identity["has_battery"] is True
    assert identity["ram_size_bytes"] == 32 * 1024
    assert identity["manufacturer_code"] == "AZL"


def test_read_rom_identity_cgb_only_and_new_licensee(tmp_path: Path) -> None:
    path = _write_rom(
        tmp_path / "cgb.gbc",
        make_rom(
            title=b"MARIO",
            cgb_flag=0xC0,
            old_licensee=0x33,
            new_licensee=b"01",
            cartridge_type=0x99,
        ),
    )
    identity = _read_rom_identity(path)
    assert identity["platform"] == "Game Boy Color (CGB only)"
    assert identity["cgb_only"] is True
    assert identity["licensee"] == "01"
    assert identity["cartridge_type"] == "unknown (0x99)"
    assert identity["has_battery"] is False
    assert identity["destination"] == "Overseas"


def test_read_rom_identity_too_small(tmp_path: Path) -> None:
    path = _write_rom(tmp_path / "tiny.gb", b"\x00" * 10)
    identity = _read_rom_identity(path)
    assert "error" in identity
    assert "too small" in identity["error"]


def test_read_rom_identity_missing_file(tmp_path: Path) -> None:
    identity = _read_rom_identity(tmp_path / "missing.gb")
    assert "error" in identity
    assert identity["kind"] == "rom"


def test_assert_rom_playable_rejects_truncated_pokemon_header(tmp_path: Path) -> None:
    rom = make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    path = _write_rom(tmp_path / "red.gb", rom)
    info = inspect_rom_playable(rom)
    assert info["playable"] is False
    assert "1024" in info["unplayable_reason"]
    assert "1048576" in info["unplayable_reason"]
    with pytest.raises(ValueError, match="1024") as excinfo:
        assert_rom_playable(path)
    assert "1048576" in str(excinfo.value)
    identity = _read_rom_identity(path)
    assert identity["playable"] is False
    assert identity["title"] == "POKEMON RED"
    assert "unplayable_reason" in identity


def test_assert_rom_playable_accepts_full_size() -> None:
    rom = make_rom()
    info = assert_rom_playable(rom)
    assert info["playable"] is True
    assert info["size_bytes"] == 32 * 1024
