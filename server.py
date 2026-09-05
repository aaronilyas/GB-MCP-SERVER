#!/usr/bin/env python3
"""Game Boy ROM MCP server.

Exposes tools that accept a ROM from the calling LLM, validate it inside an
isolated Docker container (no network, dropped capabilities), and only persist
the file under ./roms/<32-char>/ when validation succeeds. After a ROM is
accepted the server returns that subdirectory name and requests the LLM user's
email so the two can be mapped in a local SQLite database. A listing tool
returns that user's mapped subdirectories and ROM header metadata. Mapped
subdirectories can be loaded into a persistent PyBoy session (idle auto-save
and close after 45 minutes without input or ping_pyboy). There is no memory
or game-state tool; play feedback is screenshots and screenshot-derived
signals only.

Default transport is stdio (`python server.py`). Streamable HTTP is opt-in
(`python server.py --http` or GB_MCP_TRANSPORT=streamable-http) and sits
behind dual transport auth: a static bearer / operator JWT, or MCP OAuth 2.1
(authorization-code + PKCE) for hosted LLM connectors; see README.md.
"""

from __future__ import annotations

import argparse
import base64
import inspect
import re
from dataclasses import asdict
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from pydantic import Field

import db
from gb_mcp import config
from gb_mcp.config import MAX_ROM_B64_CHARS, MAX_ROM_BYTES
from gb_mcp.emulator import session as pyboy_sessions
from gb_mcp.emulator.input_schema import PlayInput, parse_emulation_speed, parse_play_input
from gb_mcp.emulator.play_limits import (
    DEFAULT_EMULATION_SPEED,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_SCREENSHOT_SCALE,
    DEFAULT_UNTIL_EVAL_INTERVAL,
    MAX_FRAMES_PER_CALL,
    MAX_GAP_FRAMES,
    MAX_HOLD_FRAMES,
    MAX_INPUT_STEPS,
    MAX_UNTIL_EVAL_INTERVAL,
    MIN_UNTIL_EVAL_INTERVAL,
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
        "(email and subdirectory name are both required; default speed is uncapped). "
        "Play with send_pyboy_input (buttons, steps, macros, optional until on the "
        "framebuffer); it returns PNG screenshot(s). There is no memory or "
        "game-state tool — until and classifiers are screenshot-derived from the "
        "native 160x144 LCD. Keep the session alive with ping_pyboy if you will "
        "think longer than about 30 seconds (does not advance emulation or press "
        "buttons). Write the cartridge save without stopping via save_battery; "
        "stop with stop_pyboy. Forty-five minutes without send_pyboy_input or "
        "ping_pyboy auto-saves the game and closes PyBoy. Read-only resources "
        "expose the owned ROM list, cartridge header metadata, and live PyBoy "
        "session status for an email. A full how-to is at the gb://usage "
        "resource; reading it is optional."
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


def _call_manager_method(method: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a SessionManager method, dropping kwargs the current signature rejects.

    C is adding extra parameters (`play_payload`, `emulation_speed`, idle) in
    parallel. Always try `play_payload` when provided.
    """
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return method(*args, **kwargs)
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
        return method(*args, **kwargs)
    accepted = {name: value for name, value in kwargs.items() if name in params}
    extra_payload = kwargs.get("play_payload")
    if extra_payload is not None and "play_payload" not in accepted:
        try:
            return method(*args, **accepted, play_payload=extra_payload)
        except TypeError:
            pass
    return method(*args, **accepted)


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
        "the user of the LLM so the subdirectory can be mapped to that user. "
        "If email is already known the subdirectory is mapped automatically. "
        "Pass boot=true to also start PyBoy after a successful map."
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
    boot: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "If true and the ROM is mapped to email, start PyBoy for that "
                "subdirectory after submit (same as load_subdirectory_rom: "
                "uncapped speed, 45-minute idle). If email is missing or "
                "mapping failed, the ROM is still saved but PyBoy is not "
                "started; follow model_request to map first."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Validate a Game Boy ROM in an isolated Docker container and save if valid.

    Args:
        rom_base64: Base64-encoded contents of the candidate .gb/.gbc file.
        filename: Preferred filename to use if the ROM is accepted (sanitized).
        email: Optional email of the LLM's user. Used to map the subdirectory
            only after the ROM is confirmed valid; omitted emails are requested
            in the tool result.
        boot: If true, start PyBoy after a successful email mapping.

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
        if boot:
            result["started"] = False
            result["running"] = False
        return result

    if boot:
        try:
            rom_path = _rom_in_subdirectory(subdirectory)
            loaded = _call_manager_method(
                pyboy_sessions.manager.load,
                result["email"],
                subdirectory,
                rom_path,
                emulation_speed=DEFAULT_EMULATION_SPEED,
                idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            result["started"] = False
            result["running"] = False
            result["error"] = f"ROM saved and mapped but PyBoy did not start: {exc}"
            return result
        result["started"] = loaded.get("started", False)
        result["running"] = loaded.get("running", False)
        for key, value in loaded.items():
            if key not in result:
                result[key] = value
        if loaded.get("error") and not result["started"]:
            result["error"] = loaded["error"]
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
        "subdirectory name are required. Default emulation_speed is 0 "
        "(uncapped). The session keeps running until stop_pyboy is called, "
        "or until about 45 minutes pass with no send_pyboy_input or "
        "ping_pyboy; idle timeout auto-saves then closes PyBoy. Call "
        "ping_pyboy if you will think longer than about 30 seconds. A later "
        "load of the same subdirectory restores that save. There is no "
        "memory or game-state tool."
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
    emulation_speed: Annotated[
        int | str,
        Field(
            default=DEFAULT_EMULATION_SPEED,
            description=(
                "PyBoy set_emulation_speed at session start. 0 or 'uncapped' "
                "(default) runs as fast as the host allows. Allowed: 0, 1, 2, "
                "4, 8, or 'uncapped'."
            ),
        ),
    ] = DEFAULT_EMULATION_SPEED,
    idle_timeout_seconds: Annotated[
        int,
        Field(
            default=DEFAULT_IDLE_TIMEOUT_SECONDS,
            description=(
                "Seconds without send_pyboy_input or ping_pyboy before the "
                "session auto-saves and closes. Default 2700 (45 minutes)."
            ),
        ),
    ] = DEFAULT_IDLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Start (or resume) a PyBoy session for an owned ROM subdirectory."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        resolved["started"] = False
        resolved["running"] = False
        return resolved

    normalized_email, name = resolved
    try:
        speed = parse_emulation_speed(emulation_speed)
    except ValueError as exc:
        return {
            "started": False,
            "running": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": str(exc),
        }
    if isinstance(idle_timeout_seconds, bool) or not isinstance(
        idle_timeout_seconds, (int, float)
    ):
        return {
            "started": False,
            "running": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": "idle_timeout_seconds must be a positive number",
        }
    idle = float(idle_timeout_seconds)
    if idle <= 0:
        return {
            "started": False,
            "running": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": "idle_timeout_seconds must be a positive number",
        }

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
        result = _call_manager_method(
            pyboy_sessions.manager.load,
            normalized_email,
            name,
            rom_path,
            emulation_speed=speed,
            idle_timeout_seconds=idle,
        )
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


def _jsonish(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonish(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonish(item) for item in value]
    if isinstance(value, list):
        return [_jsonish(item) for item in value]
    return value


def _play_to_payload(play: PlayInput) -> dict[str, Any]:
    """JSON-ish dict of the normalized PlayInput for SessionManager.send_input."""
    payload = asdict(play)
    extra = payload.pop("extra", None) or {}
    payload.update(extra)
    return _jsonish(payload)


def _play_steps(play: PlayInput) -> list[dict[str, Any]] | None:
    if not play.steps:
        return None
    return _jsonish([asdict(step) for step in play.steps])


def _play_request_dict(
    *,
    buttons: list[str] | None,
    hold_frames: int | None,
    steps: list[dict[str, Any]] | None,
    screenshot_mode: str,
    macro: str | None,
    mash_button: str | None,
    mash_press_frames: int | None,
    mash_release_frames: int | None,
    max_frames: int | None,
    gap_frames: int | None,
    wait: bool,
    emulation_speed: int | str,
    screenshot_scale: int,
    until: dict[str, Any] | None,
    until_eval_interval: int,
    disable_default_hold_abort: bool,
    hash_regions: dict[str, Any] | None,
    ocr: bool,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "wait": wait,
        "emulation_speed": emulation_speed,
        "screenshot_mode": screenshot_mode,
        "screenshot_scale": screenshot_scale,
        "until_eval_interval": until_eval_interval,
        "disable_default_hold_abort": disable_default_hold_abort,
        "ocr": ocr,
    }
    if buttons is not None:
        raw["buttons"] = buttons
    if hold_frames is not None:
        raw["hold_frames"] = hold_frames
    if steps is not None:
        raw["steps"] = steps
    if macro is not None:
        raw["macro"] = macro
    if mash_button is not None:
        raw["mash_button"] = mash_button
    if mash_press_frames is not None:
        raw["mash_press_frames"] = mash_press_frames
    if mash_release_frames is not None:
        raw["mash_release_frames"] = mash_release_frames
    if max_frames is not None:
        raw["max_frames"] = max_frames
    if gap_frames is not None:
        raw["gap_frames"] = gap_frames
    if until is not None:
        raw["until"] = until
    if hash_regions is not None:
        raw["hash_regions"] = hash_regions
    return raw


@mcp.tool(
    name="send_pyboy_input",
    description=(
        "Send Game Boy button input to a running PyBoy session and return PNG "
        "screenshot(s) of the resulting screen. Both the LLM user's email and "
        "the 32-character subdirectory name are required. One mode per call: a "
        "single chord (`buttons` + optional `hold_frames`), an ordered `steps` "
        "list, top-level `wait=true`, or `macro` hold|mash|steps|buttons. Do "
        "not pass a non-empty top-level buttons list together with any `steps` "
        "list (including an empty one). Empty wait steps "
        "(`steps: [{buttons: [], hold_frames: n}]` "
        "or `wait: true`) are allowed; top-level `buttons=[]` without wait/macro "
        "is not. Valid buttons: a, b, start, select, up, down, left, right. "
        f"At most {MAX_INPUT_STEPS} steps; hold_frames is 1..{MAX_HOLD_FRAMES}; "
        f"max_frames / total ticks {MAX_FRAMES_PER_CALL} per call; gap_frames "
        f"0..{MAX_GAP_FRAMES}. screenshot_mode: final (default), all (cap 30), "
        "interrupt_and_final, keyframes. Screenshots are nearest-neighbor "
        f"upscaled; screenshot_scale 1..4 (default {DEFAULT_SCREENSHOT_SCALE}) "
        "so native 160x144 becomes 640x576. emulation_speed defaults to 0 "
        "(uncapped); allowed 0/uncapped/1/2/4/8. until interrupts on "
        "screenshot-derived framebuffer signals only (pixel delta, region hash, "
        "coarse classifiers) in native 160x144 space. There is NO memory, WRAM, "
        "map-id, party, or game-state tool; until is screenshot-derived. "
        "Default speed is uncapped. A successful call resets the 45-minute idle "
        "timer. After 45 minutes with no send_pyboy_input or ping_pyboy the "
        "session auto-saves and PyBoy closes. If you will think longer than "
        "about 30 seconds, call ping_pyboy (does not advance emulation or press "
        "buttons)."
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
                "Single-chord Game Boy buttons to press together (macro=buttons). "
                "Each value must be one of: a, b, start, select, up, down, left, "
                "right. Omit when passing steps or wait=true. Cannot be combined "
                "with steps (including an empty list). Top-level buttons=[] "
                "without "
                "wait/macro is an error."
            ),
        ),
    ] = None,
    hold_frames: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Frames to hold a buttons chord or a top-level wait. Default 1 "
                f"for a buttons chord. Range 1..{MAX_HOLD_FRAMES}. For macro=hold, "
                "omit to use max_frames (default 3600). Ignored when using steps "
                "(each step has its own hold_frames)."
            ),
        ),
    ] = None,
    steps: Annotated[
        list[dict[str, Any]] | None,
        Field(
            default=None,
            description=(
                "Ordered input steps to apply sequentially (macro=steps). Each "
                "step is an object with 'buttons' (Game Boy buttons pressed "
                "together), optional 'hold_frames' (default 1, "
                f"max {MAX_HOLD_FRAMES}), optional 'gap_frames' (0.."
                f"{MAX_GAP_FRAMES} ticks with buttons released after the chord), "
                "and optional 'wait' (true = tick with no buttons). Empty "
                "buttons [] is a wait step. At most "
                f"{MAX_INPUT_STEPS} steps. Do not pass this together with a "
                "non-empty top-level buttons list."
            ),
        ),
    ] = None,
    screenshot_mode: Annotated[
        str,
        Field(
            default="final",
            description=(
                "Which screenshots to return. 'final' (default): one PNG after "
                "the call. 'all': one PNG after each step (cap 30, subsampled if "
                "needed). 'interrupt_and_final': PNG at until/default-hold-abort "
                "and final. 'keyframes': up to 5 images at 25/50/75/100% plus "
                "interrupt if distinct. PNGs are nearest-neighbor upscaled."
            ),
        ),
    ] = "final",
    macro: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional play mode: 'hold' (keep buttons pressed until until / "
                "default hold abort / max_frames), 'mash' (cycle mash_button), "
                "'steps', or 'buttons'. Inferred from steps / buttons / wait "
                "when omitted."
            ),
        ),
    ] = None,
    mash_button: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Button to mash when macro=mash. Default a. One of: a, b, start, "
                "select, up, down, left, right."
            ),
        ),
    ] = None,
    mash_press_frames: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                f"Frames mash_button is held each cycle (macro=mash). 1.."
                f"{MAX_HOLD_FRAMES}, default 4."
            ),
        ),
    ] = None,
    mash_release_frames: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Frames mash_button is released each cycle (macro=mash). 1.."
                f"{MAX_HOLD_FRAMES}, default 4."
            ),
        ),
    ] = None,
    max_frames: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Cap on emulator frames ticked this call (1.."
                f"{MAX_FRAMES_PER_CALL}). Default is the script length, or "
                f"{MAX_FRAMES_PER_CALL} for hold/mash."
            ),
        ),
    ] = None,
    gap_frames: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "After each chord, release then tick this many frames with no "
                f"buttons. 0..{MAX_GAP_FRAMES}, default 0."
            ),
        ),
    ] = None,
    wait: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "If true with no steps and no buttons, tick hold_frames with all "
                "buttons released (a wait step). Does not replace a non-empty "
                "buttons chord."
            ),
        ),
    ] = False,
    emulation_speed: Annotated[
        int | str,
        Field(
            default=DEFAULT_EMULATION_SPEED,
            description=(
                "pyboy.set_emulation_speed for this call. 0 or 'uncapped' "
                "(default) runs as fast as the host allows. Allowed: 0, 1, 2, "
                "4, 8, or 'uncapped'."
            ),
        ),
    ] = DEFAULT_EMULATION_SPEED,
    screenshot_scale: Annotated[
        int,
        Field(
            default=DEFAULT_SCREENSHOT_SCALE,
            description=(
                "Integer nearest-neighbor upscale of the native 160x144 LCD "
                f"before PNG encode. 1, 2, 3, or 4 (default {DEFAULT_SCREENSHOT_SCALE} "
                "→ 640x576)."
            ),
        ),
    ] = DEFAULT_SCREENSHOT_SCALE,
    until: Annotated[
        dict[str, Any] | None,
        Field(
            default=None,
            description=(
                "Optional framebuffer interrupt (screenshot-derived, not game "
                "state). Coordinates are native 160x144. Keys: region [x,y,w,h]; "
                "on = pixel_delta_above | pixel_delta_below | stable | "
                "region_hash_eq | region_hash_neq | classifier | none; "
                "threshold (default 0.08); stable_frames (default 12); hash "
                "(hex, required for region_hash_*); classifier = textbox_likely "
                "| battle_likely | start_menu_likely; classifier_polarity = "
                "appears | disappears. until.on=none disables default hold abort."
            ),
        ),
    ] = None,
    until_eval_interval: Annotated[
        int,
        Field(
            default=DEFAULT_UNTIL_EVAL_INTERVAL,
            description=(
                "Evaluate until every N frames (and on the last frame). "
                f"{MIN_UNTIL_EVAL_INTERVAL}..{MAX_UNTIL_EVAL_INTERVAL}, default "
                f"{DEFAULT_UNTIL_EVAL_INTERVAL}."
            ),
        ),
    ] = DEFAULT_UNTIL_EVAL_INTERVAL,
    disable_default_hold_abort: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "If true, do not stop a hold macro on full-screen pixel_delta "
                "vs the start-of-call frame (threshold 0.12). until.on=none "
                "also disables it."
            ),
        ),
    ] = False,
    hash_regions: Annotated[
        dict[str, Any] | None,
        Field(
            default=None,
            description=(
                "Optional extra named native 160x144 boxes {name: [x, y, w, h]} "
                "hashed every call (blake2s of native RGB, digest_size=8). "
                "Built-in names: full, bottom, center."
            ),
        ),
    ] = None,
    ocr: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "If true, run OCR on returned PNG(s). Default false. A missing "
                "engine returns ocr_text null and ocr_error 'disabled'; the "
                "input call still succeeds."
            ),
        ),
    ] = False,
) -> list[dict[str, Any] | Image] | dict[str, Any]:
    """Press buttons on a running PyBoy session, capture the screen, reset idle."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        resolved["sent"] = False
        return resolved

    normalized_email, name = resolved
    try:
        play = parse_play_input(
            _play_request_dict(
                buttons=buttons,
                hold_frames=hold_frames,
                steps=steps,
                screenshot_mode=screenshot_mode,
                macro=macro,
                mash_button=mash_button,
                mash_press_frames=mash_press_frames,
                mash_release_frames=mash_release_frames,
                max_frames=max_frames,
                gap_frames=gap_frames,
                wait=wait,
                emulation_speed=emulation_speed,
                screenshot_scale=screenshot_scale,
                until=until,
                until_eval_interval=until_eval_interval,
                disable_default_hold_abort=disable_default_hold_abort,
                hash_regions=hash_regions,
                ocr=ocr,
            )
        )
    except ValueError as exc:
        return _input_error(normalized_email, name, str(exc))

    try:
        result = _call_manager_method(
            pyboy_sessions.manager.send_input,
            normalized_email,
            name,
            steps=_play_steps(play),
            buttons=list(play.buttons) or None,
            hold_frames=play.hold_frames,
            screenshot_mode=play.screenshot_mode,
            play_payload=_play_to_payload(play),
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
    name="ping_pyboy",
    description=(
        "Reset the idle timer on a running PyBoy session without advancing "
        "emulation and without pressing buttons. Both the LLM user's email "
        "and the 32-character subdirectory name are required. Call this if "
        "you will think longer than about 30 seconds; otherwise the session "
        "auto-saves and closes after 45 minutes with no send_pyboy_input or "
        "ping_pyboy. Returns alive, idle_timeout_seconds, and "
        "seconds_since_last_input."
    ),
)
def ping_pyboy(
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
) -> dict[str, Any]:
    """Keep a PyBoy session alive without ticking or pressing buttons."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        resolved["alive"] = False
        return resolved

    normalized_email, name = resolved
    try:
        return pyboy_sessions.manager.ping(normalized_email, name)
    except Exception as exc:  # noqa: BLE001
        return {
            "alive": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": str(exc),
        }


@mcp.tool(
    name="save_battery",
    description=(
        "Write the cartridge battery/save for a running PyBoy session without "
        "stopping PyBoy. Both the LLM user's email and the 32-character "
        "subdirectory name are required. Returns saved: true. stop_pyboy still "
        "saves then stops. Idle timeout also saves then closes."
    ),
)
def save_battery(
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
) -> dict[str, Any]:
    """Write cartridge save state and leave PyBoy running."""
    resolved = _owned_subdirectory(email, subdirectory)
    if isinstance(resolved, dict):
        resolved["saved"] = False
        return resolved

    normalized_email, name = resolved
    try:
        return pyboy_sessions.manager.save_battery(normalized_email, name)
    except Exception as exc:  # noqa: BLE001
        return {
            "saved": False,
            "email": normalized_email,
            "subdirectory": name,
            "error": str(exc),
        }


@mcp.tool(
    name="stop_pyboy",
    description=(
        "Stop a running PyBoy session. Both the LLM user's email and the "
        "32-character subdirectory name are required. The game is saved "
        "before PyBoy closes. Use save_battery to write the save without "
        "stopping. Use this instead of waiting for the 45-minute idle "
        "auto-save."
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

There is no memory, WRAM, HRAM, map-id, party, or game-state tool. Play
feedback is PNG screenshots plus screenshot-derived framebuffer signals
(`until`, region hashes, coarse classifiers, optional OCR). Do not ask for
PyBoy memory peeks.

## Workflow

Typical order: submit → map → list → load → input / ping / save → stop. Skip
submit and map when the human already has mapped games.

### 1. submit_gb_rom

Provide the ROM as base64 (`rom_base64`). `filename` and `email` are optional.
Validation runs in an isolated Docker container. On success the result includes
a 32-character hexadecimal `subdirectory` name. Play is per-subdirectory.

If `email` was omitted or mapping failed, follow `model_request` in the result
and call `map_subdirectory_to_email`. Do not invent an email. If `email` is
present the subdirectory is mapped automatically. `boot=true` starts PyBoy
after a successful map (uncapped speed, 45-minute idle); if not mapped, PyBoy
is not started.

### 2. map_subdirectory_to_email

Bind that 32-character hex name to the LLM user's email. Ask the human for
their email if you do not already have it. Never invent an email.

### 3. list_subdirectories_for_email

Find that user's games. Results include cartridge header title, platform, and
other identifying metadata. Ask the human for their email if you do not
already have it.

### 4. load_subdirectory_rom

`email` and `subdirectory` are both required. Starts or resumes the play
instance for that owned subdirectory. Default `emulation_speed` is 0
(uncapped). Optional `idle_timeout_seconds` default is 2700 (45 minutes). One
live session per email: switching games saves and stops the previous instance,
then starts the new one. A later load of the same subdirectory restores that
save.

### 5. send_pyboy_input

`email` and `subdirectory` are both required. Send button chords / macros and
receive PNG screenshot(s). One mode per call: `buttons` (optional
`hold_frames`), an ordered `steps` list, top-level `wait=true`, or `macro`
hold|mash|steps|buttons — not a non-empty `buttons` list together with a
non-empty `steps` list. Empty wait steps (`steps: [{buttons: [], hold_frames:
n}]`) are allowed. Valid buttons: a, b, start, select, up, down, left, right.
At most 500 steps; `hold_frames` 1..3600. Default `emulation_speed` is 0
(uncapped). Screenshots are nearest-neighbor upscaled (`screenshot_scale`
default 4). `screenshot_mode`: final (default), all, interrupt_and_final,
keyframes.

`until` is screenshot-derived only (native 160x144 pixel delta, region hash,
coarse classifiers). There is no game-state tool.

A successful call resets the 45-minute idle timer.

### 6. ping_pyboy

`email` and `subdirectory` are both required. Resets the idle timer. Does not
advance emulation and does not press buttons. Call this if you will think
longer than about 30 seconds. Returns `alive`, `idle_timeout_seconds`,
`seconds_since_last_input`.

### 7. save_battery

`email` and `subdirectory` are both required. Writes the cartridge
battery/save and leaves PyBoy running. Returns `saved: true`. `stop_pyboy`
still saves then stops.

### 8. stop_pyboy

`email` and `subdirectory` are both required. Saves, then stops the play
instance. After about 45 minutes with no `send_pyboy_input` or `ping_pyboy`
the session also auto-saves and closes.

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
        "list, load, play, ping, save, stop). Contains no user data."
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
