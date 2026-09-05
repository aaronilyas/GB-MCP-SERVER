from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rom_builder import make_rom

VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "docker" / "validate_gb_rom.py"


def test_valid_header_passes(validator_module) -> None:
    result = validator_module.validate_gb_rom_bytes(make_rom())
    assert result["valid"] is True
    assert result["title"] == "TESTGAME"
    assert result["cgb"] is False
    assert result["header_checksum"].startswith("0x")
    assert result["size_bytes"] == 32 * 1024
    assert result["size_note"] is None


def test_logo_mismatch_fails(validator_module) -> None:
    result = validator_module.validate_gb_rom_bytes(make_rom(include_logo=False))
    assert result["valid"] is False
    assert "Nintendo logo" in result["reason"]


def test_checksum_mismatch_fails(validator_module) -> None:
    rom = bytearray(make_rom())
    rom[0x14D] = (rom[0x14D] + 1) & 0xFF
    result = validator_module.validate_gb_rom_bytes(bytes(rom))
    assert result["valid"] is False
    assert "header checksum mismatch" in result["reason"]


def test_too_small_fails(validator_module) -> None:
    result = validator_module.validate_gb_rom_bytes(b"\x00" * 10)
    assert result["valid"] is False
    assert "too small" in result["reason"]


def test_too_large_fails(validator_module, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validator_module, "MAX_ROM_BYTES", 400)
    result = validator_module.validate_gb_rom_bytes(b"\x00" * 401)
    assert result["valid"] is False
    assert "too large" in result["reason"]


def test_truncated_rom_rejected(validator_module) -> None:
    result = validator_module.validate_gb_rom_bytes(make_rom(size=0x150, rom_size_code=0x00))
    assert result["valid"] is False
    assert "truncated" in result["reason"]
    assert "336" in result["reason"] or "0x150" in result["reason"] or "32768" in result["reason"]
    assert "32768" in result["reason"]
    assert "0x00" in result["reason"]
    assert result.get("size_note") is None


def test_pokemon_header_only_rejected(validator_module) -> None:
    rom = make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    result = validator_module.validate_gb_rom_bytes(rom)
    assert result["valid"] is False
    assert "truncated" in result["reason"]
    assert "1024" in result["reason"]
    assert "1048576" in result["reason"]
    assert "0x05" in result["reason"]
    assert result.get("size_note") is None


@pytest.mark.parametrize("size", [512, 8192])
def test_pokemon_header_slices_rejected(validator_module, size: int) -> None:
    """Incident stubs: 512-byte and 8 KiB slices of a size-code 0x05 header."""
    rom = make_rom(size=size, title=b"POKEMON RED", rom_size_code=0x05)
    result = validator_module.validate_gb_rom_bytes(rom)
    assert result["valid"] is False
    assert "truncated" in result["reason"]
    assert str(size) in result["reason"]
    assert "1048576" in result["reason"]
    assert "0x05" in result["reason"]
    assert result.get("size_note") is None
    assert result.get("size_bytes") == size


def test_full_size_pokemon_header_accepted(validator_module) -> None:
    rom = make_rom(size=1024 * 1024, title=b"POKEMON RED", rom_size_code=0x05)
    result = validator_module.validate_gb_rom_bytes(rom)
    assert result["valid"] is True
    assert result["title"] == "POKEMON RED"
    assert result["size_bytes"] == 1024 * 1024
    assert result["rom_size_code"] == 0x05
    assert result["size_note"] is None


def test_padded_rom_accepted_with_size_note(validator_module) -> None:
    result = validator_module.validate_gb_rom_bytes(
        make_rom(size=32 * 1024 + 16 * 1024, rom_size_code=0x00)
    )
    assert result["valid"] is True
    assert result["size_note"] is not None
    assert "header expectation" in result["size_note"]


def test_non_bank_padding_rejected(validator_module) -> None:
    result = validator_module.validate_gb_rom_bytes(
        make_rom(size=32 * 1024 + 100, rom_size_code=0x00)
    )
    assert result["valid"] is False
    assert "16 KiB" in result["reason"] or "bank" in result["reason"]
    assert result.get("size_note") is None


def test_unrecognized_rom_size_code_rejected(validator_module) -> None:
    result = validator_module.validate_gb_rom_bytes(make_rom(rom_size_code=0xFF))
    assert result["valid"] is False
    assert "unrecognized ROM size code" in result["reason"]
    assert "0xFF" in result["reason"]
    assert result.get("size_note") is None


def test_unrecognized_rom_size_code_allowed_with_env(
    validator_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GB_ROM_ALLOW_UNKNOWN_SIZE", "1")
    result = validator_module.validate_gb_rom_bytes(make_rom(rom_size_code=0xFF))
    assert result["valid"] is True
    assert "unrecognized ROM size code" in result["size_note"]


def test_validate_gb_rom_path(validator_module, tmp_path: Path) -> None:
    path = tmp_path / "rom.gb"
    path.write_bytes(make_rom())
    result = validator_module.validate_gb_rom(path)
    assert result["valid"] is True


def test_validate_gb_rom_missing_path(validator_module, tmp_path: Path) -> None:
    result = validator_module.validate_gb_rom(tmp_path / "nope.gb")
    assert result["valid"] is False
    assert result["reason"] == "path is not a file"


def test_main_stdin_valid() -> None:
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), "-"],
        input=make_rom(),
        capture_output=True,
        check=False,
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["valid"] is True


def test_main_rejects_bad_usage() -> None:
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH)],
        capture_output=True,
        check=False,
        text=True,
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["valid"] is False
    assert "usage" in payload["reason"]


def test_cgb_flag_reported(validator_module) -> None:
    result = validator_module.validate_gb_rom_bytes(make_rom(cgb_flag=0x80))
    assert result["valid"] is True
    assert result["cgb"] is True
