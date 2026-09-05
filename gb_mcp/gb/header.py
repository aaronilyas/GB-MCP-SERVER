"""Parse identifying fields from a stored Game Boy cartridge header."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from gb_mcp.gb.constants import (
    CARTRIDGE_BATTERY_TYPES,
    CARTRIDGE_TYPES,
    MAX_ROM_BYTES,
    MIN_ROM_BYTES,
    RAM_SIZE_BYTES,
    ROM_BANK_BYTES,
    ROM_SIZE_BYTES,
)


def _decode_header_text(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0]
    return "".join(chr(b) for b in raw if 32 <= b < 127).strip()


def _allow_unknown_rom_size() -> bool:
    return os.environ.get("GB_ROM_ALLOW_UNKNOWN_SIZE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def evaluate_rom_size(*, size: int, rom_size_code: int | None) -> dict[str, Any]:
    """Compare file length to cartridge header 0x0148.

    Truncated dumps (``size < expected``) are unplayable. Bytes past the
    expected size are allowed only as a whole-bank pad (multiple of 16 KiB).
    Unrecognized size codes are unplayable unless ``GB_ROM_ALLOW_UNKNOWN_SIZE``
    is set.
    """
    result: dict[str, Any] = {
        "playable": True,
        "unplayable_reason": None,
        "size_bytes": size,
        "rom_size_code": rom_size_code,
        "expected_rom_bytes": None,
        "size_note": None,
    }
    if size < MIN_ROM_BYTES:
        result["playable"] = False
        result["unplayable_reason"] = (
            f"file too small ({size} bytes); need at least {MIN_ROM_BYTES}"
        )
        return result
    if size > MAX_ROM_BYTES:
        result["playable"] = False
        result["unplayable_reason"] = (
            f"file too large ({size} bytes); max {MAX_ROM_BYTES}"
        )
        return result
    if rom_size_code is None:
        result["playable"] = False
        result["unplayable_reason"] = (
            f"file too small ({size} bytes); need at least {MIN_ROM_BYTES}"
        )
        return result

    expected = ROM_SIZE_BYTES.get(rom_size_code)
    result["expected_rom_bytes"] = expected
    if expected is None:
        note = f"unrecognized ROM size code 0x{rom_size_code:02X}"
        if _allow_unknown_rom_size():
            result["size_note"] = note
            return result
        result["playable"] = False
        result["unplayable_reason"] = (
            f"{note}; expected a cartridge size code 0x00–0x08. "
            "Re-submit a complete dump or set GB_ROM_ALLOW_UNKNOWN_SIZE=1"
        )
        return result
    if size < expected:
        result["playable"] = False
        result["unplayable_reason"] = (
            f"ROM is truncated ({size} bytes; header size code "
            f"0x{rom_size_code:02X} expects {expected}). "
            "Re-submit the complete .gb."
        )
        return result
    if size > expected:
        extra = size - expected
        if extra % ROM_BANK_BYTES != 0:
            result["playable"] = False
            result["unplayable_reason"] = (
                f"ROM is padded by {extra} bytes which is not a whole 16 KiB "
                f"bank (header size code 0x{rom_size_code:02X} expects {expected})"
            )
            return result
        result["size_note"] = f"size {size} != header expectation {expected}"
    return result


def inspect_rom_playable(source: Path | str | bytes) -> dict[str, Any]:
    """Return playability fields for ROM bytes or a stored file.

    Reads at most the cartridge header from a path (plus ``stat`` size) so a
    host-side check does not load an 8 MiB image just to compare lengths.
    """
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        code = data[0x148] if len(data) > 0x148 else None
        return evaluate_rom_size(size=len(data), rom_size_code=code)

    path = Path(source)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {
            "playable": False,
            "unplayable_reason": f"could not read ROM: {exc}",
            "size_bytes": None,
            "rom_size_code": None,
            "expected_rom_bytes": None,
            "size_note": None,
        }
    try:
        with path.open("rb") as fh:
            header = fh.read(0x149)
    except OSError as exc:
        return {
            "playable": False,
            "unplayable_reason": f"could not read ROM: {exc}",
            "size_bytes": size,
            "rom_size_code": None,
            "expected_rom_bytes": None,
            "size_note": None,
        }
    code = header[0x148] if len(header) > 0x148 else None
    return evaluate_rom_size(size=size, rom_size_code=code)


def assert_rom_playable(path_or_bytes: Path | str | bytes) -> dict[str, Any]:
    """Raise ``ValueError`` with the size reason if the ROM cannot be booted."""
    info = inspect_rom_playable(path_or_bytes)
    if not info.get("playable"):
        raise ValueError(info.get("unplayable_reason") or "ROM is not playable")
    return info


def _read_rom_identity(path: Path) -> dict[str, Any]:
    """Parse enough of a stored .gb/.gbc header to identify the game."""
    try:
        with path.open("rb") as fh:
            header = fh.read(0x150)
    except OSError as exc:
        return {"kind": "rom", "error": f"could not read ROM header: {exc}"}

    if len(header) < 0x150:
        return {
            "kind": "rom",
            "error": f"file too small to contain a Game Boy header ({len(header)} bytes)",
        }

    cgb_flag = header[0x143]
    cgb = bool(cgb_flag & 0x80)
    cgb_only = cgb_flag == 0xC0
    # CGB uses 0x0143 as the CGB flag, so the printable title is at most 15 chars.
    title_end = 0x143 if cgb else 0x144
    title = _decode_header_text(header[0x134:title_end])
    manufacturer = _decode_header_text(header[0x13F:0x143]) if cgb else ""
    cart_code = header[0x147]
    rom_size_code = header[0x148]
    ram_size_code = header[0x149]
    destination = header[0x14A]
    old_licensee = header[0x14B]
    if old_licensee == 0x33:
        licensee = _decode_header_text(header[0x144:0x146]) or "0x33"
    else:
        licensee = f"0x{old_licensee:02X}"

    if cgb_only:
        platform = "Game Boy Color (CGB only)"
    elif cgb:
        platform = "Game Boy Color (GB compatible)"
    else:
        platform = "Game Boy"

    playability = inspect_rom_playable(path)
    identity: dict[str, Any] = {
        "kind": "rom",
        "title": title or None,
        "platform": platform,
        "cgb": cgb,
        "cgb_only": cgb_only,
        "sgb": header[0x146] == 0x03,
        "manufacturer_code": manufacturer or None,
        "licensee": licensee,
        "cartridge_type": CARTRIDGE_TYPES.get(cart_code, f"unknown (0x{cart_code:02X})"),
        "cartridge_type_code": cart_code,
        "has_battery": cart_code in CARTRIDGE_BATTERY_TYPES,
        "rom_size_code": rom_size_code,
        "rom_size_bytes": ROM_SIZE_BYTES.get(rom_size_code),
        "ram_size_code": ram_size_code,
        "ram_size_bytes": RAM_SIZE_BYTES.get(ram_size_code, 0),
        "destination": "Japan" if destination == 0x00 else "Overseas",
        "mask_rom_version": header[0x14C],
        "playable": bool(playability.get("playable")),
    }
    if playability.get("unplayable_reason"):
        identity["unplayable_reason"] = playability["unplayable_reason"]
    if playability.get("size_note"):
        identity["size_note"] = playability["size_note"]
    return identity
