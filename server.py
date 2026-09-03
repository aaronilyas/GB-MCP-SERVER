#!/usr/bin/env python3
"""Game Boy ROM MCP server.

Exposes tools that accept a ROM from the calling LLM, validate it inside an
isolated Docker container (no network, dropped capabilities), and only persist
the file under ./roms/<32-char>/ when validation succeeds. After a ROM is
accepted the server returns that subdirectory name and requests the LLM user's
email so the two can be mapped in a local SQLite database. A listing tool
returns that user's mapped subdirectories and ROM header metadata.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

import db

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
        "confirmed .gb/.gbc files under a unique 32-character subdirectory of "
        "roms/. After a ROM passes validation the server returns that "
        "subdirectory name and requests the email address of the user of the "
        "LLM. Map the two with map_subdirectory_to_email. List a user's mapped "
        "ROM subdirectories and game metadata with list_subdirectories_for_email."
    ),
)

# Cartridge header 0x0147 (Pan Docs). Used so listing can say what mapper/saves a ROM has.
_CARTRIDGE_TYPES: dict[int, str] = {
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
_CARTRIDGE_BATTERY_TYPES = {
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
_ROM_SIZE_BYTES = {
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
_RAM_SIZE_BYTES = {
    0x00: 0,
    0x01: 2 * 1024,
    0x02: 8 * 1024,
    0x03: 32 * 1024,
    0x04: 128 * 1024,
    0x05: 64 * 1024,
}
_ROM_SUFFIXES = {".gb", ".gbc"}


def _sanitize_filename(name: str) -> str:
    base = Path(name).name.strip() or "rom.gb"
    base = re.sub(r"[^\w.\-]+", "_", base)
    if not base.lower().endswith((".gb", ".gbc")):
        base = f"{base}.gb"
    return base[:180]


def _optional_email(email: str | None) -> str | None:
    if email is None:
        return None
    value = email.strip()
    return value or None


def _allocate_subdirectory_name() -> str:
    """Pick a 32-character name that is unused on disk and in the mapping DB."""
    ROMS_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        name = db.new_subdirectory_name()
        if (ROMS_DIR / name).exists():
            continue
        with db.session_scope() as session:
            taken = db.subdirectory_exists(session, name)
        if not taken:
            return name
    raise RuntimeError("failed to allocate a unique 32-character subdirectory name")


def _persist_validated_rom(subdirectory: str, safe_name: str, rom_bytes: bytes) -> Path:
    dest_dir = ROMS_DIR / subdirectory
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    if dest.exists():
        dest = dest_dir / f"{dest.stem}-{uuid.uuid4().hex[:8]}{dest.suffix}"

    # Persist only after in-container validation succeeded.
    # Write via a same-directory temp file then replace for atomicity.
    fd, tmp_name = tempfile.mkstemp(prefix=".rom-", suffix=".tmp", dir=dest_dir)
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
    return dest


def _email_model_request(subdirectory: str) -> dict[str, str]:
    return {
        "name": "email",
        "instruction": (
            "Provide the email address of the user of the LLM so subdirectory "
            f"{subdirectory} can be mapped to that user. Call "
            "map_subdirectory_to_email with the subdirectory name and email."
        ),
    }


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


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
        "cartridge_type": _CARTRIDGE_TYPES.get(cart_code, f"unknown (0x{cart_code:02X})"),
        "cartridge_type_code": cart_code,
        "has_battery": cart_code in _CARTRIDGE_BATTERY_TYPES,
        "rom_size_code": rom_size_code,
        "rom_size_bytes": _ROM_SIZE_BYTES.get(rom_size_code),
        "ram_size_code": ram_size_code,
        "ram_size_bytes": _RAM_SIZE_BYTES.get(ram_size_code, 0),
        "destination": "Japan" if destination == 0x00 else "Overseas",
        "mask_rom_version": header[0x14C],
    }


def _iter_subdirectory_files(dest: Path) -> list[Path]:
    dest_resolved = dest.resolve()
    files: list[Path] = []
    for path in dest.iterdir():
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved.name.startswith("."):
            continue
        if not resolved.is_relative_to(dest_resolved):
            continue
        files.append(resolved)
    files.sort(key=lambda p: p.name.lower())
    return files


def _describe_subdirectory(name: str, created_at: datetime | None) -> dict[str, Any]:
    dest = ROMS_DIR / name
    info: dict[str, Any] = {
        "subdirectory": name,
        "path": f"roms/{name}",
        "created_at": _isoformat(created_at),
        "exists_on_disk": False,
        "files": [],
        "games": [],
    }
    try:
        info["exists_on_disk"] = dest.is_dir()
        if not info["exists_on_disk"]:
            info["summary"] = "mapped in the database but missing from disk"
            return info

        files: list[dict[str, Any]] = []
        games: list[dict[str, Any]] = []
        for path in _iter_subdirectory_files(dest):
            stat = path.stat()
            entry: dict[str, Any] = {
                "filename": path.name,
                "path": str(path.relative_to(ROOT)),
                "size_bytes": stat.st_size,
                "modified_at": _isoformat(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
            }
            if path.suffix.lower() in _ROM_SUFFIXES:
                identity = _read_rom_identity(path)
                entry.update(identity)
                # Truncated/unreadable .gb/.gbc stay in files, not in games.
                if "error" not in identity:
                    games.append(
                        {
                            "title": identity.get("title"),
                            "filename": path.name,
                            "platform": identity.get("platform"),
                            "cartridge_type": identity.get("cartridge_type"),
                            "has_battery": identity.get("has_battery"),
                            "size_bytes": stat.st_size,
                        }
                    )
            else:
                entry["kind"] = "other"
            files.append(entry)

        info["files"] = files
        info["games"] = games
        titles = [g["title"] for g in games if g.get("title")]
        if not files:
            info["summary"] = "empty subdirectory"
        elif titles:
            unique = list(dict.fromkeys(titles))
            n = len(files)
            info["summary"] = f"{', '.join(unique)} ({n} file{'s' if n != 1 else ''})"
        else:
            info["summary"] = ", ".join(f["filename"] for f in files)
        return info
    except (OSError, ValueError) as exc:
        info["error"] = str(exc)
        info["summary"] = "could not be read"
        return info


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
        "the container; the container is removed afterward. If validation "
        "succeeds the file is saved under roms/<32-character-subdirectory>/ "
        "and the server returns that name and requests the email address of "
        "the user of the LLM so the subdirectory can be mapped to that user."
    ),
)
def submit_gb_rom(
    rom_base64: str,
    filename: str = "rom.gb",
    email: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional. Email address of the user of the LLM if you already "
                "have it. After a ROM passes Game Boy validation the server "
                "returns a 32-character subdirectory name; if this is omitted "
                "it also requests the address so you can call "
                "map_subdirectory_to_email."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Validate a Game Boy ROM in an isolated Docker container and save if valid.

    Args:
        rom_base64: Base64-encoded contents of the candidate .gb/.gbc file.
        filename: Preferred filename to use if the ROM is accepted (sanitized).
        email: Optional email of the LLM's user. Used to map the subdirectory
            only after the ROM is confirmed valid; omitted emails are requested
            in the tool result.

    Returns:
        A dict describing acceptance, save path, 32-character subdirectory,
        email mapping status, and validator details.
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
            "subdirectory": None,
            "mapped": False,
            "validation": validation,
            "error": validation.get("reason", "not a valid Game Boy ROM"),
        }

    try:
        subdirectory = _allocate_subdirectory_name()
        dest = _persist_validated_rom(subdirectory, safe_name, rom_bytes)
    except Exception as exc:  # noqa: BLE001
        return {
            "accepted": False,
            "saved": False,
            "path": None,
            "subdirectory": None,
            "mapped": False,
            "validation": validation,
            "error": f"failed to save ROM: {exc}",
        }

    result: dict[str, Any] = {
        "accepted": True,
        "saved": True,
        "path": str(dest.relative_to(ROOT)),
        "subdirectory": subdirectory,
        "mapped": False,
        "validation": validation,
    }

    provided_email = _optional_email(email)
    if provided_email is not None:
        try:
            with db.session_scope() as session:
                mapped = db.map_subdirectory_to_email(session, subdirectory, provided_email)
                result["email"] = mapped.user.email
            result["mapped"] = True
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"ROM saved but email could not be mapped: {exc}"

    if not result["mapped"]:
        result["model_request"] = _email_model_request(subdirectory)
    return result


@mcp.tool(
    name="map_subdirectory_to_email",
    description=(
        "Map a 32-character ROM subdirectory name (returned by submit_gb_rom "
        "after a ROM passes Game Boy validation) to the email address of the "
        "user of the LLM. Call this after submit_gb_rom returns a subdirectory "
        "and a request for the user's email."
    ),
)
def map_subdirectory_to_email(
    subdirectory: Annotated[
        str,
        Field(
            description=(
                "The 32-character subdirectory name returned by submit_gb_rom "
                "after a successful Game Boy ROM validation."
            )
        ),
    ],
    email: Annotated[
        str,
        Field(
            description=(
                "Email address of the user of the LLM. Ask the user for this "
                "if you do not already have it."
            )
        ),
    ],
) -> dict[str, Any]:
    """Persist the mapping between a ROM subdirectory and the user's email."""
    name = subdirectory.strip().lower()
    if len(name) != db.SUBDIRECTORY_NAME_LENGTH or not re.fullmatch(r"[0-9a-f]+", name):
        return {
            "mapped": False,
            "subdirectory": subdirectory,
            "error": (
                f"subdirectory must be a {db.SUBDIRECTORY_NAME_LENGTH}-character "
                "hexadecimal name returned by submit_gb_rom"
            ),
        }
    if not (ROMS_DIR / name).is_dir():
        return {
            "mapped": False,
            "subdirectory": name,
            "error": f"subdirectory {name!r} does not exist under roms/",
        }

    try:
        with db.session_scope() as session:
            mapped = db.map_subdirectory_to_email(session, name, email)
            normalized_email = mapped.user.email
    except Exception as exc:  # noqa: BLE001
        return {
            "mapped": False,
            "subdirectory": name,
            "model_request": _email_model_request(name),
            "error": str(exc),
        }

    return {
        "mapped": True,
        "subdirectory": name,
        "email": normalized_email,
    }


@mcp.tool(
    name="list_subdirectories_for_email",
    description=(
        "List ROM subdirectories mapped to the email address of the user of "
        "the LLM, with metadata that identifies which game each subdirectory "
        "holds (title from the cartridge header, platform, mapper, battery, "
        "file names and sizes). Call this when you need to find an existing "
        "game directory for that user. Ask the user for their email if you "
        "do not already have it."
    ),
)
def list_subdirectories_for_email(
    email: Annotated[
        str,
        Field(
            description=(
                "Email address of the user of the LLM who owns the ROM "
                "subdirectories. Ask the user for this if you do not already "
                "have it."
            )
        ),
    ],
) -> dict[str, Any]:
    """Return mapped roms/ subdirectories and identifying game metadata for an email."""
    try:
        with db.session_scope() as session:
            rows = db.list_subdirectories_for_email(session, email)
            mapped = [(row.name, row.created_at) for row in rows]
            normalized_email = db.normalize_email(email)

        subdirectories = [
            _describe_subdirectory(name, created_at) for name, created_at in mapped
        ]
        return {
            "email": normalized_email,
            "count": len(subdirectories),
            "subdirectories": subdirectories,
        }
    except Exception as exc:  # noqa: BLE001 - surface clean MCP error
        return {
            "email": email,
            "count": 0,
            "subdirectories": [],
            "error": str(exc),
        }


if __name__ == "__main__":
    db.init_db()
    mcp.run(transport="stdio")
