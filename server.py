#!/usr/bin/env python3
"""Game Boy ROM MCP server.

Exposes a tool that accepts a ROM from the calling LLM, validates it inside an
isolated Docker container (no network, dropped capabilities), and only persists
the file under ./roms/ when validation succeeds.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

ROOT = Path(__file__).resolve().parent
ROMS_DIR = ROOT / "roms"
DOCKER_IMAGE = os.environ.get("GB_ROM_VALIDATOR_IMAGE", "gb-rom-validator:latest")
MAX_ROM_BYTES = 8 * 1024 * 1024
# Base64 expands 3 bytes -> 4 chars; reject before decode to bound host memory.
MAX_ROM_B64_CHARS = (MAX_ROM_BYTES + 2) // 3 * 4

mcp = MCPServer(
    "gb-mcp-server",
    instructions=(
        "Accepts Game Boy ROM binaries from the model, validates them inside an "
        "isolated Docker container with no internet access, and saves only "
        "confirmed .gb/.gbc files under the local roms/ subdirectory."
    ),
)


def _sanitize_filename(name: str) -> str:
    base = Path(name).name.strip() or "rom.gb"
    base = re.sub(r"[^\w.\-]+", "_", base)
    if not base.lower().endswith((".gb", ".gbc")):
        base = f"{base}.gb"
    return base[:180]


def _docker_available() -> None:
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Docker is required and must be running to validate ROMs in isolation."
        ) from exc


def _ensure_image() -> None:
    probe = subprocess.run(
        ["docker", "image", "inspect", DOCKER_IMAGE],
        capture_output=True,
        timeout=30,
    )
    if probe.returncode == 0:
        return
    build = subprocess.run(
        ["docker", "build", "-t", DOCKER_IMAGE, str(ROOT)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if build.returncode != 0:
        raise RuntimeError(
            f"Failed to build Docker image {DOCKER_IMAGE}:\n"
            f"{build.stderr or build.stdout}"
        )


def _run_docker(args: list[str], *, input_bytes: bytes | None = None, timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", *args],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )


def _create_isolated_container() -> str:
    """Start a locked-down container before any ROM bytes are loaded into it."""
    name = f"gb-rom-validate-{uuid.uuid4().hex[:12]}"
    create = _run_docker(
        [
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--tmpfs",
            "/work:rw,noexec,nosuid,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            DOCKER_IMAGE,
            "sleep",
            "infinity",
        ],
        timeout=60,
    )
    if create.returncode != 0:
        raise RuntimeError(
            f"Failed to create isolated container: {create.stderr.decode(errors='replace')}"
        )
    container_id = create.stdout.decode().strip()
    start = _run_docker(["start", container_id], timeout=60)
    if start.returncode != 0:
        _run_docker(["rm", "-f", container_id], timeout=30)
        raise RuntimeError(
            f"Failed to start isolated container: {start.stderr.decode(errors='replace')}"
        )
    return container_id


def _validate_inside_container(container_id: str, rom_bytes: bytes) -> dict[str, Any]:
    """Load ROM bytes into the already-running container via stdin and validate.

    The ROM is not written on the host before the isolated container exists; it is
    streamed into `docker exec` only after the container is up with --network=none.
    """
    result = _run_docker(
        [
            "exec",
            "-i",
            container_id,
            "python3",
            "/opt/validator/validate_gb_rom.py",
            "-",
        ],
        input_bytes=rom_bytes,
        timeout=60,
    )
    stdout = result.stdout.decode(errors="replace").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        payload = {
            "valid": False,
            "reason": f"validator returned non-JSON output: {stdout!r}",
        }
    if result.returncode != 0 and "valid" not in payload:
        payload = {
            "valid": False,
            "reason": (
                stdout
                or result.stderr.decode(errors="replace")
                or f"validator exited {result.returncode}"
            ),
        }
    return payload


def _destroy_container(container_id: str) -> None:
    try:
        _run_docker(["rm", "-f", container_id], timeout=30)
    except Exception:
        # Teardown must not mask validation results or crash the MCP tool.
        pass


@mcp.tool(
    name="submit_gb_rom",
    description=(
        "Submit a Game Boy / Game Boy Color ROM for isolated validation. "
        "Provide the ROM as base64. A Docker container with no internet access "
        "is started first; the ROM is loaded into that container only after it "
        "is running; validation (Nintendo logo + header checksum) runs inside "
        "the container; the container is removed afterward. The file is saved "
        "under roms/ only if validation succeeds."
    ),
)
def submit_gb_rom(rom_base64: str, filename: str = "rom.gb") -> dict[str, Any]:
    """Validate a Game Boy ROM in an isolated Docker container and save if valid.

    Args:
        rom_base64: Base64-encoded contents of the candidate .gb/.gbc file.
        filename: Preferred filename to use if the ROM is accepted (sanitized).

    Returns:
        A dict describing acceptance, save path (when valid), and validator details.
    """
    if len(rom_base64) > MAX_ROM_B64_CHARS:
        return {
            "accepted": False,
            "saved": False,
            "path": None,
            "error": (
                f"ROM payload exceeds maximum encoded size of {MAX_ROM_B64_CHARS} "
                f"base64 characters ({MAX_ROM_BYTES} bytes decoded)"
            ),
        }

    try:
        rom_bytes = base64.b64decode(rom_base64, validate=True)
    except Exception as exc:  # noqa: BLE001 - surface clean MCP error
        return {
            "accepted": False,
            "saved": False,
            "path": None,
            "error": f"invalid base64 ROM payload: {exc}",
        }

    if not rom_bytes:
        return {
            "accepted": False,
            "saved": False,
            "path": None,
            "error": "ROM payload is empty",
        }
    if len(rom_bytes) > MAX_ROM_BYTES:
        return {
            "accepted": False,
            "saved": False,
            "path": None,
            "error": f"ROM exceeds maximum size of {MAX_ROM_BYTES} bytes",
        }

    safe_name = _sanitize_filename(filename)
    container_id: str | None = None

    try:
        _docker_available()
        _ensure_image()

        # Isolation first: bring up a network-less container, then stream the ROM in.
        container_id = _create_isolated_container()
        validation = _validate_inside_container(container_id, rom_bytes)
    except Exception as exc:  # noqa: BLE001
        return {
            "accepted": False,
            "saved": False,
            "path": None,
            "error": str(exc),
        }
    finally:
        if container_id:
            _destroy_container(container_id)

    if not validation.get("valid"):
        return {
            "accepted": False,
            "saved": False,
            "path": None,
            "validation": validation,
            "error": validation.get("reason", "not a valid Game Boy ROM"),
        }

    try:
        ROMS_DIR.mkdir(parents=True, exist_ok=True)
        dest = ROMS_DIR / safe_name
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            dest = ROMS_DIR / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"

        # Persist only after in-container validation succeeded.
        # Write via a same-directory temp file then replace for atomicity.
        fd, tmp_name = tempfile.mkstemp(prefix=".rom-", suffix=".tmp", dir=ROMS_DIR)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(rom_bytes)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, dest)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001
        return {
            "accepted": False,
            "saved": False,
            "path": None,
            "validation": validation,
            "error": f"failed to save ROM: {exc}",
        }

    return {
        "accepted": True,
        "saved": True,
        "path": str(dest.relative_to(ROOT)),
        "validation": validation,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
