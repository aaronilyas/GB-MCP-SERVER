#!/usr/bin/env python3
"""Game Boy ROM MCP server.

Exposes tools that accept a ROM from the calling LLM, validate it inside an
isolated Docker container (no network, dropped capabilities), and only persist
the file under ./roms/<32-char>/ when validation succeeds. After a ROM is
accepted the server returns that subdirectory name and requests the LLM user's
email so the two can be mapped in a local SQLite database. A listing tool
returns that user's mapped subdirectories and ROM header metadata. Mapped
subdirectories can be loaded into a persistent PyBoy session (idle auto-save
and close after 5 minutes without input).
"""

from __future__ import annotations

import base64
import re
from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

import db
from gb_mcp import config
from gb_mcp.config import MAX_ROM_B64_CHARS, MAX_ROM_BYTES
from gb_mcp.emulator import session as pyboy_sessions
from gb_mcp.emulator.session import BUTTONS, MAX_HOLD_FRAMES
from gb_mcp.isolation.docker import (
    _create_isolated_container,
    _destroy_container,
    _docker_available,
    _ensure_image,
    _validate_inside_container,
)
from gb_mcp.storage.roms import (
    _allocate_subdirectory_name,
    _describe_subdirectory,
    _persist_validated_rom,
    _rom_in_subdirectory,
    _sanitize_filename,
)

mcp = MCPServer(
    "gb-mcp-server",
    instructions=(
        "Accepts Game Boy ROM binaries from the model, validates them inside an "
        "isolated Docker container with no internet access, and saves only "
        "confirmed .gb/.gbc files under a unique 32-character subdirectory of "
        "roms/. After a ROM passes validation the server returns that "
        "subdirectory name and requests the email address of the user of the "
        "LLM. Map the two with map_subdirectory_to_email. List a user's mapped "
        "ROM subdirectories and game metadata with list_subdirectories_for_email. "
        "Load a mapped subdirectory's ROM into PyBoy with load_subdirectory_rom "
        "(email and subdirectory name are both required). Keep the session alive "
        "by sending buttons with send_pyboy_input; stop it with stop_pyboy. "
        "Five minutes without button input auto-saves the game and closes PyBoy."
    ),
)


def _optional_email(email: str | None) -> str | None:
    if email is None:
        return None
    value = email.strip()
    return value or None


def _email_model_request(subdirectory: str) -> dict[str, str]:
    return {
        "name": "email",
        "instruction": (
            "Provide the email address of the user of the LLM so subdirectory "
            f"{subdirectory} can be mapped to that user. Call "
            "map_subdirectory_to_email with the subdirectory name and email."
        ),
    }


def _subdirectory_name(subdirectory: str) -> str:
    name = subdirectory.strip().lower()
    if len(name) != db.SUBDIRECTORY_NAME_LENGTH or not re.fullmatch(r"[0-9a-f]+", name):
        raise ValueError(
            f"subdirectory must be a {db.SUBDIRECTORY_NAME_LENGTH}-character "
            "hexadecimal name returned by submit_gb_rom"
        )
    return name


def _owned_subdirectory(email: str, subdirectory: str) -> tuple[str, str] | dict[str, Any]:
    """Validate email + subdirectory and confirm the mapping exists.

    Returns (normalized_email, name) on success, or an error dict.
    """
    try:
        normalized_email = db.normalize_email(email)
        name = _subdirectory_name(subdirectory)
    except ValueError as exc:
        return {
            "email": email,
            "subdirectory": subdirectory,
            "error": str(exc),
        }

    try:
        with db.session_scope() as session:
            row = db.get_subdirectory_for_email(session, name, normalized_email)
    except Exception as exc:  # noqa: BLE001
        return {
            "email": normalized_email,
            "subdirectory": name,
            "error": str(exc),
        }

    if row is None:
        return {
            "email": normalized_email,
            "subdirectory": name,
            "error": f"subdirectory {name!r} is not mapped to {normalized_email}",
        }
    if not (config.ROMS_DIR / name).is_dir():
        return {
            "email": normalized_email,
            "subdirectory": name,
            "error": f"subdirectory {name!r} does not exist under roms/",
        }
    return normalized_email, name


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
        "path": str(dest.relative_to(config.ROOT)),
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
    try:
        name = _subdirectory_name(subdirectory)
    except ValueError as exc:
        return {
            "mapped": False,
            "subdirectory": subdirectory,
            "error": str(exc),
        }
    if not (config.ROMS_DIR / name).is_dir():
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


@mcp.tool(
    name="load_subdirectory_rom",
    description=(
        "Load a mapped ROM subdirectory and start a persistent PyBoy session "
        "for that game. Both the LLM user's email and the 32-character "
        "subdirectory name are required. The session keeps running until "
        "stop_pyboy is called, or until about 5 minutes pass with no button "
        "input from send_pyboy_input; idle timeout auto-saves then closes "
        "PyBoy. A later load of the same subdirectory restores that save."
    ),
)
def load_subdirectory_rom(
    email: Annotated[
        str,
        Field(
            description=(
                "Email address of the user of the LLM who owns the ROM "
                "subdirectory. Ask the user for this if you do not already "
                "have it."
            )
        ),
    ],
    subdirectory: Annotated[
        str,
        Field(
            description=(
                "The 32-character subdirectory name returned by submit_gb_rom "
                "and mapped with map_subdirectory_to_email."
            )
        ),
    ],
) -> dict[str, Any]:
    """Start (or resume) a PyBoy session for an owned ROM subdirectory."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        resolved["started"] = False
        resolved["running"] = False
        return resolved

    normalized_email, name = resolved
    try:
        rom_path = _rom_in_subdirectory(name)
    except Exception as exc:  # noqa: BLE001
        return {
            "started": False,
            "running": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": str(exc),
        }

    try:
        result = pyboy_sessions.manager.load(normalized_email, name, rom_path)
    except Exception as exc:  # noqa: BLE001
        return {
            "started": False,
            "running": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": f"failed to start PyBoy: {exc}",
        }
    return result


@mcp.tool(
    name="send_pyboy_input",
    description=(
        "Send Game Boy button input to a running PyBoy session. Both the LLM "
        "user's email and the 32-character subdirectory name are required. "
        "Valid buttons: a, b, start, select, up, down, left, right. Any "
        "successful call resets the 5-minute idle timer. After 5 minutes "
        "with no input the session auto-saves and PyBoy closes."
    ),
)
def send_pyboy_input(
    email: Annotated[
        str,
        Field(
            description=(
                "Email address of the user of the LLM who owns the running "
                "PyBoy session. Ask the user for this if you do not already "
                "have it."
            )
        ),
    ],
    subdirectory: Annotated[
        str,
        Field(
            description=(
                "The 32-character subdirectory name of the running PyBoy session."
            )
        ),
    ],
    buttons: Annotated[
        list[str],
        Field(
            description=(
                "Game Boy buttons to press together this step. Each value must "
                "be one of: a, b, start, select, up, down, left, right."
            )
        ),
    ],
    hold_frames: Annotated[
        int,
        Field(
            default=1,
            description=(
                "How many emulator frames to hold the buttons before release. "
                f"Must be between 1 and {MAX_HOLD_FRAMES}."
            ),
        ),
    ] = 1,
) -> dict[str, Any]:
    """Press buttons on a running PyBoy session and reset the idle timer."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        resolved["sent"] = False
        return resolved

    normalized_email, name = resolved
    if not buttons:
        return {
            "sent": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": "at least one button is required",
        }

    normalized_buttons: list[str] = []
    for button in buttons:
        value = button.strip().lower()
        if value not in BUTTONS:
            return {
                "sent": False,
                "email": normalized_email,
                "subdirectory": name,
                "error": (
                    f"invalid button {button!r}; expected one of "
                    f"{', '.join(sorted(BUTTONS))}"
                ),
            }
        normalized_buttons.append(value)

    if not isinstance(hold_frames, int) or hold_frames < 1 or hold_frames > MAX_HOLD_FRAMES:
        return {
            "sent": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": f"hold_frames must be an integer from 1 to {MAX_HOLD_FRAMES}",
        }

    try:
        return pyboy_sessions.manager.send_input(
            normalized_email, name, normalized_buttons, hold_frames
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "sent": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": str(exc),
        }


@mcp.tool(
    name="stop_pyboy",
    description=(
        "Stop a running PyBoy session. Both the LLM user's email and the "
        "32-character subdirectory name are required. The game is saved "
        "before PyBoy closes. Use this instead of waiting for the 5-minute "
        "idle auto-save."
    ),
)
def stop_pyboy(
    email: Annotated[
        str,
        Field(
            description=(
                "Email address of the user of the LLM who owns the running "
                "PyBoy session. Ask the user for this if you do not already "
                "have it."
            )
        ),
    ],
    subdirectory: Annotated[
        str,
        Field(
            description=(
                "The 32-character subdirectory name of the PyBoy session to stop."
            )
        ),
    ],
) -> dict[str, Any]:
    """Save and close a running PyBoy session."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        resolved["stopped"] = False
        return resolved

    normalized_email, name = resolved
    try:
        return pyboy_sessions.manager.stop(normalized_email, name)
    except Exception as exc:  # noqa: BLE001
        return {
            "stopped": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": str(exc),
        }


if __name__ == "__main__":
    db.init_db()
    mcp.run(transport="stdio")
