"""The isolated validator must keep copies of the shared host constants."""

from __future__ import annotations

import pytest

from gb_mcp.gb import constants
from gb_mcp.gb.header import evaluate_rom_size

from rom_builder import make_rom


def test_validator_constants_match_shared_source(validator_module) -> None:
    assert validator_module.NINTENDO_LOGO == constants.NINTENDO_LOGO
    assert validator_module.ROM_SIZE_BYTES == constants.ROM_SIZE_BYTES
    assert validator_module.MIN_ROM_BYTES == constants.MIN_ROM_BYTES
    assert validator_module.MAX_ROM_BYTES == constants.MAX_ROM_BYTES
    assert validator_module.ROM_BANK_BYTES == constants.ROM_BANK_BYTES


def test_nintendo_logo_is_48_bytes() -> None:
    assert len(constants.NINTENDO_LOGO) == 48


@pytest.mark.parametrize(
    "kwargs",
    [
        {"size": 0x150, "rom_size_code": 0x00},
        {"size": 32 * 1024 + 100, "rom_size_code": 0x00},
        {"rom_size_code": 0xFF},
    ],
    ids=["truncated", "non_bank_pad", "unknown_size_code"],
)
def test_size_policy_reject_reasons_match_header(validator_module, kwargs) -> None:
    """Validator reject strings must match evaluate_rom_size on the same bytes.

    docker/validate_gb_rom.py stays a single script; do not import gb_mcp there.
    """
    rom = make_rom(**kwargs)
    validator = validator_module.validate_gb_rom_bytes(rom)
    header = evaluate_rom_size(size=len(rom), rom_size_code=rom[0x148])
    assert validator["valid"] is False
    assert header["playable"] is False
    assert validator["reason"] == header["unplayable_reason"]
