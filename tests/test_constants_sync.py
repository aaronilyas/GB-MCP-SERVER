"""The isolated validator must keep copies of the shared host constants."""

from __future__ import annotations

from gb_mcp.gb import constants


def test_validator_constants_match_shared_source(validator_module) -> None:
    assert validator_module.NINTENDO_LOGO == constants.NINTENDO_LOGO
    assert validator_module.ROM_SIZE_BYTES == constants.ROM_SIZE_BYTES
    assert validator_module.MIN_ROM_BYTES == constants.MIN_ROM_BYTES
    assert validator_module.MAX_ROM_BYTES == constants.MAX_ROM_BYTES
    assert validator_module.ROM_BANK_BYTES == constants.ROM_BANK_BYTES


def test_nintendo_logo_is_48_bytes() -> None:
    assert len(constants.NINTENDO_LOGO) == 48
