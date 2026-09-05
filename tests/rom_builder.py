"""Build minimal Game Boy ROM images for tests."""

from __future__ import annotations

from gb_mcp.gb.constants import NINTENDO_LOGO, ROM_SIZE_BYTES


def make_rom(
    *,
    size: int | None = None,
    title: bytes = b"TESTGAME",
    cgb_flag: int = 0x00,
    sgb_flag: int = 0x00,
    cartridge_type: int = 0x00,
    rom_size_code: int = 0x00,
    ram_size_code: int = 0x00,
    destination: int = 0x01,
    old_licensee: int = 0x01,
    new_licensee: bytes = b"01",
    mask_rom_version: int = 0x00,
    include_logo: bool = True,
    fix_checksum: bool = True,
    checksum: int | None = None,
) -> bytes:
    if size is None:
        size = ROM_SIZE_BYTES.get(rom_size_code, 32 * 1024)
    data = bytearray(size)
    if include_logo and size >= 0x134:
        data[0x104:0x134] = NINTENDO_LOGO
    if size >= 0x144:
        title_bytes = title[:16]
        data[0x134 : 0x134 + len(title_bytes)] = title_bytes
    if size > 0x143:
        data[0x143] = cgb_flag
    if old_licensee == 0x33 and size >= 0x146:
        data[0x144:0x146] = new_licensee[:2].ljust(2, b"0")
    if size > 0x146:
        data[0x146] = sgb_flag
    if size > 0x147:
        data[0x147] = cartridge_type
    if size > 0x148:
        data[0x148] = rom_size_code
    if size > 0x149:
        data[0x149] = ram_size_code
    if size > 0x14A:
        data[0x14A] = destination
    if size > 0x14B:
        data[0x14B] = old_licensee
    if size > 0x14C:
        data[0x14C] = mask_rom_version
    if size > 0x14D:
        if checksum is not None:
            data[0x14D] = checksum
        elif fix_checksum:
            value = 0
            for byte in data[0x134:0x14D]:
                value = (value - byte - 1) & 0xFF
            data[0x14D] = value
    return bytes(data)
