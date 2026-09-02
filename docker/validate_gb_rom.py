#!/usr/bin/env python3
"""Validate a candidate Game Boy / Game Boy Color ROM header.

Runs only inside the isolated gb-rom-validator container. Exits 0 when the
file passes Nintendo logo + header checksum checks; exits 1 otherwise.
Prints a single JSON object to stdout describing the result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Nintendo logo bitmap required at cartridge header 0x0104–0x0133 (Pan Docs).
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

MIN_ROM_BYTES = 0x150
MAX_ROM_BYTES = 8 * 1024 * 1024


def _title(data: bytes) -> str:
    raw = data[0x134:0x144]
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def validate_gb_rom_bytes(data: bytes) -> dict:
    size = len(data)
    if size < MIN_ROM_BYTES:
        return {
            "valid": False,
            "reason": f"file too small ({size} bytes); need at least {MIN_ROM_BYTES}",
        }
    if size > MAX_ROM_BYTES:
        return {
            "valid": False,
            "reason": f"file too large ({size} bytes); max {MAX_ROM_BYTES}",
        }

    logo = data[0x104:0x134]
    if logo != NINTENDO_LOGO:
        return {"valid": False, "reason": "Nintendo logo mismatch at 0x0104–0x0133"}

    # Header checksum over 0x0134–0x014C must match byte at 0x014D.
    checksum = 0
    for byte in data[0x134:0x14D]:
        checksum = (checksum - byte - 1) & 0xFF
    if checksum != data[0x14D]:
        return {
            "valid": False,
            "reason": (
                f"header checksum mismatch: computed 0x{checksum:02X}, "
                f"stored 0x{data[0x14D]:02X}"
            ),
        }

    rom_size_code = data[0x148]
    expected = ROM_SIZE_BYTES.get(rom_size_code)
    size_note = None
    if expected is None:
        size_note = f"unrecognized ROM size code 0x{rom_size_code:02X}"
    elif size != expected:
        # Homebrew / padded dumps sometimes differ; logo+checksum already passed.
        size_note = f"size {size} != header expectation {expected}"

    cgb_flag = data[0x143]
    is_cgb = bool(cgb_flag & 0x80)

    return {
        "valid": True,
        "reason": "valid Game Boy ROM header",
        "title": _title(data),
        "size_bytes": size,
        "cgb": is_cgb,
        "rom_size_code": rom_size_code,
        "size_note": size_note,
        "header_checksum": f"0x{data[0x14D]:02X}",
    }


def validate_gb_rom(path: Path) -> dict:
    if not path.is_file():
        return {"valid": False, "reason": "path is not a file"}
    return validate_gb_rom_bytes(path.read_bytes())


def main() -> int:
    if len(sys.argv) != 2:
        print(
            json.dumps(
                {
                    "valid": False,
                    "reason": "usage: validate_gb_rom.py <path|->  (- reads stdin)",
                }
            ),
            flush=True,
        )
        return 1

    target = sys.argv[1]
    if target == "-":
        result = validate_gb_rom_bytes(sys.stdin.buffer.read())
    else:
        result = validate_gb_rom(Path(target))
    print(json.dumps(result), flush=True)
    return 0 if result.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
