"""Parse identifying fields from a stored Game Boy cartridge header."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gb_mcp.gb.constants import (
    CARTRIDGE_BATTERY_TYPES,
    CARTRIDGE_TYPES,
    RAM_SIZE_BYTES,
    ROM_SIZE_BYTES,
)


def _decode_header_text(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0]
    return "".join(chr(b) for b in raw if 32 <= b < 127).strip()


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

    return {
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
    }
