"""Shared Game Boy cartridge constants (Pan Docs).

This module is the host source of truth. docker/validate_gb_rom.py keeps its
own copies so the isolated image stays a single self-contained script; tests
assert those copies match the values here.
"""

from __future__ import annotations

# Nintendo logo bitmap required at cartridge header 0x0104–0x0133.
NINTENDO_LOGO = bytes(
    [
        0xCE, 0xED, 0x66, 0x66, 0xCC, 0x0D, 0x00, 0x0B,
        0x03, 0x73, 0x00, 0x83, 0x00, 0x0C, 0x00, 0x0D,
        0x00, 0x08, 0x11, 0x1F, 0x88, 0x89, 0x00, 0x0E,
        0xDC, 0xCC, 0x6E, 0xE6, 0xDD, 0xDD, 0xD9, 0x99,
        0xBB, 0xBB, 0x67, 0x63, 0x6E, 0x0E, 0xEC, 0xCC,
        0xDD, 0xDC, 0x99, 0x9F, 0xBB, 0xB9, 0x33, 0x3E,
    ]
)

MIN_ROM_BYTES = 0x150
MAX_ROM_BYTES = 8 * 1024 * 1024

# Cartridge header 0x0147. Used so listing can say what mapper/saves a ROM has.
CARTRIDGE_TYPES: dict[int, str] = {
    0x00: "ROM only",
    0x01: "MBC1",
    0x02: "MBC1+RAM",
    0x03: "MBC1+RAM+BATTERY",
    0x05: "MBC2",
    0x06: "MBC2+BATTERY",
    0x08: "ROM+RAM",
    0x09: "ROM+RAM+BATTERY",
    0x0B: "MMM01",
    0x0C: "MMM01+RAM",
    0x0D: "MMM01+RAM+BATTERY",
    0x0F: "MBC3+TIMER+BATTERY",
    0x10: "MBC3+TIMER+RAM+BATTERY",
    0x11: "MBC3",
    0x12: "MBC3+RAM",
    0x13: "MBC3+RAM+BATTERY",
    0x19: "MBC5",
    0x1A: "MBC5+RAM",
    0x1B: "MBC5+RAM+BATTERY",
    0x1C: "MBC5+RUMBLE",
    0x1D: "MBC5+RUMBLE+RAM",
    0x1E: "MBC5+RUMBLE+RAM+BATTERY",
    0x20: "MBC6",
    0x22: "MBC7+SENSOR+RUMBLE+RAM+BATTERY",
    0xFC: "POCKET CAMERA",
    0xFD: "BANDAI TAMA5",
    0xFE: "HuC3",
    0xFF: "HuC1+RAM+BATTERY",
}

CARTRIDGE_BATTERY_TYPES = {
    0x03,
    0x06,
    0x09,
    0x0D,
    0x0F,
    0x10,
    0x13,
    0x1B,
    0x1E,
    0x22,
    0xFC,  # Pocket Camera
    0xFE,  # HuC3
    0xFF,
}

# Valid ROM sizes in bytes (cartridge header 0x0148 codes 0x00–0x08).
ROM_SIZE_BYTES = {
    0x00: 32 * 1024,
    0x01: 64 * 1024,
    0x02: 128 * 1024,
    0x03: 256 * 1024,
    0x04: 512 * 1024,
    0x05: 1024 * 1024,
    0x06: 2 * 1024 * 1024,
    0x07: 4 * 1024 * 1024,
    0x08: 8 * 1024 * 1024,
}

RAM_SIZE_BYTES = {
    0x00: 0,
    0x01: 2 * 1024,
    0x02: 8 * 1024,
    0x03: 32 * 1024,
    0x04: 128 * 1024,
    0x05: 64 * 1024,
}

ROM_SUFFIXES = {".gb", ".gbc"}
