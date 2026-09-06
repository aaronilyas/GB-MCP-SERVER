"""Model-facing play tools: list, boot, play, save, stop."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.utilities.types import Image

from gb_mcp.emulator import session as pyboy_sessions
from gb_mcp.emulator.input_schema import PlayInput, parse_play_args, play_input_from_args
from gb_mcp.emulator.loop import shape_public_status
from gb_mcp.gb.header import assert_rom_playable
from gb_mcp.identity import require_email
from gb_mcp.storage.roms import _rom_in_subdirectory
from gb_mcp.storage.uploads import expire_uploads
from gb_mcp.tools.catalog import list_games as catalog_list_games


def _jsonish(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonish(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonish(item) for item in value]
    if isinstance(value, list):
        return [_jsonish(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def play_to_payload(play: PlayInput) -> dict[str, Any]:
    """JSON-ish dict of the normalized PlayInput for SessionManager.send_input."""
    payload = asdict(play)
    extra = payload.pop("extra", None) or {}
    payload.update(extra)
    return _jsonish(payload)


def _public(internal: dict[str, Any]) -> dict[str, Any]:
    return shape_public_status(internal)


def _unplayable_boot_error(reason: str) -> str:
    text = reason.strip()
    if text.endswith("Re-submit the complete .gb."):
        text = text[: -len("Re-submit the complete .gb.")].rstrip()
    text = text.rstrip(".")
    return f"{text}. Re-submit the complete .gb via add_rom or HTTP POST /roms."


def list_games() -> dict[str, Any]:
    bound = require_email()
    if isinstance(bound, dict):
        return bound
    expire_uploads()
    return catalog_list_games(bound)


def boot(
    title: str | None = None,
    id: str | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    bound = require_email()
    if isinstance(bound, dict):
        return bound
    resolved = pyboy_sessions.manager.resolve_game(bound, title=title, id=id)
    if resolved.get("error"):
        payload = _public(
            {
                "ok": False,
                "running": False,
                "stopped": True,
                "error": resolved["error"],
                "cartridge_title": resolved.get("title"),
            }
        )
        if "matches" in resolved:
            payload["matches"] = resolved["matches"]
        return payload

    name = str(resolved["subdirectory"])
    try:
        rom_path = _rom_in_subdirectory(name)
        assert_rom_playable(rom_path)
    except FileNotFoundError as exc:
        return _public(
            {
                "ok": False,
                "running": False,
                "stopped": True,
                "error": str(exc),
                "cartridge_title": resolved.get("title"),
            }
        )
    except ValueError as exc:
        return _public(
            {
                "ok": False,
                "running": False,
                "stopped": True,
                "error": _unplayable_boot_error(str(exc)),
                "cartridge_title": resolved.get("title"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _public(
            {
                "ok": False,
                "running": False,
                "stopped": True,
                "error": str(exc),
                "cartridge_title": resolved.get("title"),
            }
        )

    try:
        if reset:
            result = pyboy_sessions.manager.reset(
                bound,
                name,
                rom_path,
                discard_state=True,
                restore_state=False,
            )
        else:
            result = pyboy_sessions.manager.load(
                bound,
                name,
                rom_path,
                restore_state=True,
            )
    except Exception as exc:  # noqa: BLE001
        return _public(
            {
                "ok": False,
                "running": False,
                "stopped": True,
                "error": f"failed to start PyBoy: {exc}",
                "cartridge_title": resolved.get("title"),
            }
        )
    return _public(result)


def _running_session(email: str) -> Any | dict[str, Any]:
    current = pyboy_sessions.manager.current(email)
    if isinstance(current, dict):
        return _public(current)
    return current


def play(
    buttons: list[str] | None = None,
    frames: int | None = None,
    gap: int | None = None,
    mash: bool | None = None,
    steps: list[dict[str, Any]] | None = None,
    until: str | None = None,
    media: str | None = None,
) -> list[dict[str, Any] | Image] | dict[str, Any]:
    bound = require_email()
    if isinstance(bound, dict):
        return bound
    session = _running_session(bound)
    if isinstance(session, dict):
        return session

    raw: dict[str, Any] = {}
    if buttons is not None:
        raw["buttons"] = buttons
    if frames is not None:
        raw["frames"] = frames
    if gap is not None:
        raw["gap"] = gap
    if mash is not None:
        raw["mash"] = mash
    if steps is not None:
        raw["steps"] = steps
    if until is not None:
        raw["until"] = until
    if media is not None:
        raw["media"] = media
    try:
        args = parse_play_args(raw)
        play_input = play_input_from_args(args)
    except ValueError as exc:
        return _public(
            {
                "ok": False,
                "sent": False,
                "running": True,
                "error": str(exc),
                "cartridge_title": session.cartridge_title,
            }
        )

    try:
        result = pyboy_sessions.manager.send_input(
            bound,
            session.subdirectory,
            play_payload=play_to_payload(play_input),
        )
    except Exception as exc:  # noqa: BLE001
        return _public(
            {
                "ok": False,
                "sent": False,
                "running": session.is_running,
                "error": str(exc),
                "cartridge_title": session.cartridge_title,
            }
        )

    pngs = result.pop("pngs", []) or []
    gif = result.pop("gif", None)
    result.pop("gifs", None)
    result.pop("gif_b64", None)
    result.pop("pngs_b64", None)
    status = _public(result)
    if not result.get("sent") and result.get("error"):
        return status

    want_video = args.media == "video" or bool(gif)
    image: Image | None = None
    if want_video and isinstance(gif, (bytes, bytearray)) and gif:
        image = Image(data=bytes(gif), format="gif")
    elif pngs:
        image = Image(data=pngs[-1], format="png")
    if image is None:
        return status
    return [status, image]


def save() -> dict[str, Any]:
    bound = require_email()
    if isinstance(bound, dict):
        return bound
    session = _running_session(bound)
    if isinstance(session, dict):
        return session
    try:
        result = pyboy_sessions.manager.save_battery(bound, session.subdirectory)
    except Exception as exc:  # noqa: BLE001
        return _public(
            {
                "ok": False,
                "running": session.is_running,
                "error": str(exc),
                "cartridge_title": session.cartridge_title,
            }
        )
    return _public(result)


def stop() -> dict[str, Any]:
    bound = require_email()
    if isinstance(bound, dict):
        return bound
    session = _running_session(bound)
    if isinstance(session, dict):
        return session
    try:
        result = pyboy_sessions.manager.stop(bound, session.subdirectory)
    except Exception as exc:  # noqa: BLE001
        return _public(
            {
                "ok": False,
                "stopped": False,
                "running": session.is_running,
                "error": str(exc),
                "cartridge_title": session.cartridge_title,
            }
        )
    return _public(result)


def capture_screen_png() -> bytes | dict[str, Any]:
    """Return the current LCD PNG, or a public error dict."""
    bound = require_email()
    if isinstance(bound, dict):
        return bound
    session = _running_session(bound)
    if isinstance(session, dict):
        return session
    try:
        args = parse_play_args({"buttons": [], "frames": 1})
        play_input = play_input_from_args(args)
        result = pyboy_sessions.manager.send_input(
            bound,
            session.subdirectory,
            play_payload=play_to_payload(play_input),
        )
    except Exception as exc:  # noqa: BLE001
        return _public(
            {
                "ok": False,
                "running": session.is_running,
                "error": str(exc),
                "cartridge_title": session.cartridge_title,
            }
        )
    pngs = result.pop("pngs", []) or []
    if not pngs:
        return _public(result)
    return pngs[-1]
