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

Default transport is stdio (`python server.py`). Streamable HTTP is opt-in
(`python server.py --http` or GB_MCP_TRANSPORT=streamable-http) and sits
behind a bearer token; see README.md.
"""

from __future__ import annotations

import argparse
import base64
import re
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from pydantic import Field

import db
from gb_mcp import config
from gb_mcp.config import MAX_ROM_B64_CHARS, MAX_ROM_BYTES
from gb_mcp.emulator import session as pyboy_sessions
from gb_mcp.emulator.session import (
    BUTTONS,
    MAX_HOLD_FRAMES,
    MAX_INPUT_STEPS,
    SCREENSHOT_MODES,
)
from gb_mcp.http import attach_public_routes, run_http
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
        "by sending a button chord or a sequence of chords with send_pyboy_input, "
        "which returns PNG screenshot(s) of the resulting screen; stop it with "
        "stop_pyboy. Five minutes without button input auto-saves the game and "
        "closes PyBoy. Read-only resources expose the owned ROM list, cartridge "
        "header metadata, and live PyBoy session status for an email. "
        "A full how-to is at the gb://usage resource; reading it is optional."
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


def _input_error(email: str, subdirectory: str, error: str) -> dict[str, Any]:
    return {
        "sent": False,
        "email": email,
        "subdirectory": subdirectory,
        "error": error,
    }


def _normalize_buttons(buttons: Any) -> list[str]:
    if not isinstance(buttons, list) or not buttons:
        raise ValueError("at least one button is required")
    normalized: list[str] = []
    for button in buttons:
        if not isinstance(button, str):
            raise ValueError(
                f"invalid button {button!r}; expected one of "
                f"{', '.join(sorted(BUTTONS))}"
            )
        value = button.strip().lower()
        if value not in BUTTONS:
            raise ValueError(
                f"invalid button {button!r}; expected one of "
                f"{', '.join(sorted(BUTTONS))}"
            )
        normalized.append(value)
    return normalized


def _normalize_hold_frames(hold_frames: Any) -> int:
    if not isinstance(hold_frames, int) or hold_frames < 1 or hold_frames > MAX_HOLD_FRAMES:
        raise ValueError(f"hold_frames must be an integer from 1 to {MAX_HOLD_FRAMES}")
    return hold_frames


def _normalize_steps(steps: list[Any]) -> list[dict[str, Any]]:
    if not steps:
        raise ValueError("steps must not be empty")
    if len(steps) > MAX_INPUT_STEPS:
        raise ValueError(f"steps cannot exceed {MAX_INPUT_STEPS}")
    normalized: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(
                f"step {index}: each step must be an object with a buttons list"
            )
        try:
            buttons = _normalize_buttons(step.get("buttons"))
            hold_frames = _normalize_hold_frames(step.get("hold_frames", 1))
        except ValueError as exc:
            raise ValueError(f"step {index}: {exc}") from exc
        normalized.append({"buttons": buttons, "hold_frames": hold_frames})
    return normalized


@mcp.tool(
    name="send_pyboy_input",
    description=(
        "Send Game Boy button input to a running PyBoy session and return PNG "
        "screenshot(s) of the resulting screen. Both the LLM user's email and "
        "the 32-character subdirectory name are required. Pass either a single "
        "chord as buttons (optional hold_frames), or an ordered steps list of "
        "chords to run one after another in this call — the length of steps is "
        "how many inputs run before screenshots come back. Do not pass both a "
        "non-empty buttons list and a non-empty steps list. screenshot_mode "
        "'final' (default) returns one PNG after all steps; 'all' returns one "
        "PNG after each step, in order. Valid buttons: a, b, start, select, "
        f"up, down, left, right. At most {MAX_INPUT_STEPS} steps; hold_frames "
        f"is 1..{MAX_HOLD_FRAMES}. Any successful call resets the 5-minute idle "
        "timer. After 5 minutes with no input the session auto-saves and PyBoy "
        "closes."
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
        list[str] | None,
        Field(
            default=None,
            description=(
                "Single-step Game Boy buttons to press together. Each value must "
                "be one of: a, b, start, select, up, down, left, right. Omit when "
                "passing steps. Cannot be combined with a non-empty steps list."
            ),
        ),
    ] = None,
    hold_frames: Annotated[
        int,
        Field(
            default=1,
            description=(
                "How many emulator frames to hold the top-level buttons before "
                f"release. Must be between 1 and {MAX_HOLD_FRAMES}. Ignored when "
                "using steps (each step has its own hold_frames)."
            ),
        ),
    ] = 1,
    steps: Annotated[
        list[dict[str, Any]] | None,
        Field(
            default=None,
            description=(
                "Ordered input steps to apply sequentially in this call. Each "
                "step is an object with 'buttons' (non-empty list of Game Boy "
                "buttons pressed together) and optional 'hold_frames' (default 1, "
                f"max {MAX_HOLD_FRAMES}). At most {MAX_INPUT_STEPS} steps. The "
                "length of this list is how many inputs run before screenshots "
                "are returned. Do not pass this together with a non-empty "
                "top-level buttons list."
            ),
        ),
    ] = None,
    screenshot_mode: Annotated[
        str,
        Field(
            default="final",
            description=(
                "Which screenshots to return. 'final' (default): one PNG after "
                "all steps. 'all': one PNG after each step, in order."
            ),
        ),
    ] = "final",
) -> list[dict[str, Any] | Image] | dict[str, Any]:
    """Press buttons on a running PyBoy session, capture the screen, reset idle."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        resolved["sent"] = False
        return resolved

    normalized_email, name = resolved
    mode = screenshot_mode.strip().lower() if isinstance(screenshot_mode, str) else screenshot_mode
    if mode not in SCREENSHOT_MODES:
        return _input_error(
            normalized_email,
            name,
            "screenshot_mode must be 'final' or 'all'",
        )

    has_buttons = bool(buttons)
    try:
        if steps is not None:
            if has_buttons and len(steps) > 0:
                return _input_error(
                    normalized_email,
                    name,
                    "provide either top-level buttons or steps, not both",
                )
            payload_steps = _normalize_steps(steps)
        elif has_buttons:
            payload_steps = [
                {
                    "buttons": _normalize_buttons(buttons),
                    "hold_frames": _normalize_hold_frames(hold_frames),
                }
            ]
        else:
            return _input_error(
                normalized_email,
                name,
                "at least one button is required",
            )
    except ValueError as exc:
        return _input_error(normalized_email, name, str(exc))

    try:
        result = pyboy_sessions.manager.send_input(
            normalized_email,
            name,
            steps=payload_steps,
            screenshot_mode=mode,
        )
    except Exception as exc:  # noqa: BLE001
        return _input_error(normalized_email, name, str(exc))

    if not result.get("sent"):
        result.pop("pngs", None)
        return result

    pngs = result.pop("pngs", [])
    images = [Image(data=png, format="png") for png in pngs]
    return [result, *images]


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


_USAGE_GUIDE = """# How to use this Game Boy MCP server

This is a how-to for the connected model. Reading it is optional: every tool
works even if you never read `gb://usage`. This resource contains no user data.

Email is application identity, not transport authentication. Never put a
bearer token, signed token, login secret, API key, or Docker credential in a
tool argument. Never guess, enumerate, or probe other users' emails or
subdirectory names. Only submit a ROM the human provided; do not scrape the
host filesystem or send unrelated files. Subdirectory names are the
32-character hex strings returned by `submit_gb_rom`, not game titles.

## Workflow

Typical order: submit → map → list → load → input → stop. Skip submit and map
when the human already has mapped games.

### 1. submit_gb_rom

Provide the ROM as base64 (`rom_base64`). `filename` and `email` are optional.
Validation runs in an isolated Docker container. On success the result includes
a 32-character hexadecimal `subdirectory` name. Play is per-subdirectory.

If `email` was omitted or mapping failed, follow `model_request` in the result
and call `map_subdirectory_to_email`. Do not invent an email.

### 2. map_subdirectory_to_email

Bind that 32-character hex name to the LLM user's email. Ask the human for
their email if you do not already have it. Never invent an email.

### 3. list_subdirectories_for_email

Find that user's games. Results include cartridge header title, platform, and
other identifying metadata. Ask the human for their email if you do not
already have it.

### 4. load_subdirectory_rom

`email` and `subdirectory` are both required. Starts or resumes the play
instance for that owned subdirectory. One live session per email: switching
games saves and stops the previous instance, then starts the new one. A later
load of the same subdirectory restores that save.

### 5. send_pyboy_input

`email` and `subdirectory` are both required. Send button chords and receive
PNG screenshot(s). Pass either a single chord as `buttons` (optional
`hold_frames`) or an ordered `steps` list — not both. Valid buttons: a, b,
start, select, up, down, left, right. `screenshot_mode` `final` (default)
returns one PNG after all steps; `all` returns one PNG after each step.
A successful call resets the 5-minute idle timer.

### 6. stop_pyboy

`email` and `subdirectory` are both required. Saves, then stops the play
instance. After about 5 minutes with no button input the session also
auto-saves and closes.

## Read-only data resources

These URIs are live user data and require an email. They are not a substitute
for this guide:

- `gb://users/{email}/roms` — owned ROM list and game metadata
- `gb://users/{email}/roms/{subdirectory}` — cartridge header metadata for an owned ROM
- `gb://users/{email}/session` — live play-instance status for that email
"""


@mcp.resource(
    "gb://usage",
    mime_type="text/markdown",
    description=(
        "How a connected model should use this Game Boy MCP server (submit, map, "
        "list, load, play, stop). Contains no user data."
    ),
)
def usage_resource() -> str:
    """Return the static model-facing how-to. No user data, no I/O."""
    return _USAGE_GUIDE


@mcp.resource(
    "gb://users/{email}/roms",
    mime_type="application/json",
    description=(
        "Read-only list of ROM subdirectories mapped to the LLM user's email, "
        "with cartridge title/platform metadata. Email is application identity, "
        "not transport authentication."
    ),
)
def owned_roms_resource(email: str) -> dict[str, Any]:
    """List owned ROM subdirectories for an email."""
    return list_subdirectories_for_email(email)


@mcp.resource(
    "gb://users/{email}/roms/{subdirectory}",
    mime_type="application/json",
    description=(
        "Read-only cartridge header metadata for a ROM subdirectory owned by "
        "the given email. Both email and the 32-character subdirectory name "
        "are required."
    ),
)
def rom_header_resource(email: str, subdirectory: str) -> dict[str, Any]:
    """Return header metadata for an owned ROM subdirectory."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        return resolved
    normalized_email, name = resolved
    created_at = None
    try:
        with db.session_scope() as session:
            row = db.get_subdirectory_for_email(session, name, normalized_email)
            if row is not None:
                created_at = row.created_at
    except Exception as exc:  # noqa: BLE001
        return {
            "email": normalized_email,
            "subdirectory": name,
            "error": str(exc),
        }
    info = _describe_subdirectory(name, created_at)
    info["email"] = normalized_email
    return info


@mcp.resource(
    "gb://users/{email}/session",
    mime_type="application/json",
    description=(
        "Read-only live PyBoy session status for the LLM user identified by "
        "email (running flag, ROM, idle timer). Email is application identity."
    ),
)
def session_status_resource(email: str) -> dict[str, Any]:
    """Return the live PyBoy session status for an email, if any."""
    try:
        normalized_email = db.normalize_email(email)
    except ValueError as exc:
        return {"email": email, "running": False, "error": str(exc)}
    session = pyboy_sessions.manager.get(normalized_email)
    if session is None:
        return {"email": normalized_email, "running": False}
    return session.status()


attach_public_routes(mcp)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Game Boy ROM MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve Streamable HTTP on GB_MCP_PATH (default /mcp) instead of stdio",
    )
    args = parser.parse_args(argv)
    db.init_db()
    if args.http or config.http_transport_requested():
        run_http(mcp)
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

