"""Process-wide paths and limits for the host MCP server."""

from __future__ import annotations

import os
from pathlib import Path

from gb_mcp.gb.constants import MAX_ROM_BYTES

ROOT = Path(__file__).resolve().parent.parent
ROMS_DIR = ROOT / "roms"
DOCKER_IMAGE = os.environ.get("GB_ROM_VALIDATOR_IMAGE", "gb-rom-validator:latest")
# Base64 expands 3 bytes -> 4 chars; reject before decode to bound host memory.
MAX_ROM_B64_CHARS = (MAX_ROM_BYTES + 2) // 3 * 4
