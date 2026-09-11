"""Model-facing play tools: list, boot, play, save, stop."""

from __future__ import annotations

import base64
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.utilities.types import Image

from gb_mcp.emulator import session as pyboy_sessions
from gb_mcp.emulator.input_schema import (
    PlayInput,
    parse_play_args,
    parse_screenshot_mode,
    parse_screenshot_scale,
    play_input_from_args,
)
from gb_mcp.emulator.loop import shape_public_status
from gb_mcp.emulator.play_limits import MAX_SCREENSHOT_ALL, NATIVE_HEIGHT, NATIVE_WIDTH
from gb_mcp.gb.header import assert_rom_playable
from gb_mcp.identity import require_email
from gb_mcp.storage.roms import _rom_in_subdirectory
from gb_mcp.storage.uploads import expire_uploads
from gb_mcp.tools.catalog import list_games as catalog_list_games

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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


def _png_ihdr_size(blob: bytes) -> tuple[int, int] | None:
    """Read width/height from a PNG IHDR without importing Pillow."""
    if len(blob) < 24 or not blob.startswith(_PNG_MAGIC):
        return None
    if blob[12:16] != b"IHDR":
        return None
    width = int.from_bytes(blob[16:20], "big")
    height = int.from_bytes(blob[20:24], "big")
    return width, height


def _as_png_list(value: Any) -> list[bytes]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[bytes] = []
    for item in items:
        if isinstance(item, (bytes, bytearray)) and item:
            out.append(bytes(item))
    return out


def _decode_png_b64_list(value: Any) -> list[bytes]:
    if not isinstance(value, list):
        return []
    out: list[bytes] = []
    for item in value:
        if not isinstance(item, str) or not item:
            continue
        try:
            decoded = base64.b64decode(item)
        except Exception:
            continue
        if decoded:
            out.append(decoded)
    return out


def _native_pngs(result: dict[str, Any]) -> list[bytes]:
    """Native 160x144 PNG bytes for public JSON. Never use the scaled preview."""
    blobs = _as_png_list(result.get("pngs_native"))
    if not blobs:
        blobs = _decode_png_b64_list(result.get("pngs_native_b64"))
    if not blobs:
        blobs = _as_png_list(result.get("pngs"))
    if not blobs:
        blobs = _decode_png_b64_list(result.get("pngs_b64"))
    native: list[bytes] = []
    for blob in blobs:
        if _png_ihdr_size(blob) != (NATIVE_WIDTH, NATIVE_HEIGHT):
            continue
        native.append(blob)
        if len(native) >= MAX_SCREENSHOT_ALL:
            break
    return native


def _screenshot_entries(pngs: list[bytes]) -> list[dict[str, Any]]:
    return [
        {
            "png_base64": base64.b64encode(blob).decode("ascii"),
            "width": NATIVE_WIDTH,
            "height": NATIVE_HEIGHT,
            "scale": 1,
        }
        for blob in pngs
    ]


def _final_image_png(pngs: list[bytes], native: list[bytes]) -> bytes | None:
    """Last preview PNG (already at the requested scale) or last native LCD."""
    if pngs:
        return pngs[-1]
    if native:
        return native[-1]
    return None


def _public_screenshot_list(
    native: list[bytes],
    meta: list[dict[str, Any]] | None,
    *,
    mode: str = "final",
) -> list[dict[str, Any]]:
    """One final native frame by default; richer lists for keyframes/all."""
    if not native:
        return []
    kinds: list[str] = []
    if isinstance(meta, list) and meta:
        kinds = [str(item.get("kind") or "") for item in meta]
    entries = _screenshot_entries(native)
    if mode in {"keyframes", "all"}:
        out: list[dict[str, Any]] = []
        for index, entry in enumerate(entries[:MAX_SCREENSHOT_ALL]):
            labeled = dict(entry)
            kind = kinds[index] if index < len(kinds) else ""
            if index == len(entries) - 1:
                labeled["kind"] = kind or "final"
            else:
                labeled["kind"] = kind or "keyframe"
            out.append(labeled)
        return out
    if len(entries) == 1:
        entries[0]["kind"] = "final"
        return entries
    # Keep interrupt + final when packaging recorded both; otherwise only final.
    if len(entries) >= 2 and any("interrupt" in kind for kind in kinds):
        out: list[dict[str, Any]] = []
        # Pair from the end: last is final; earlier interrupt if labeled.
        for index, entry in enumerate(entries):
            kind = kinds[index] if index < len(kinds) else ""
            labeled = dict(entry)
            if "interrupt" in kind and index < len(entries) - 1:
                labeled["kind"] = "interrupt"
                out.append(labeled)
            elif index == len(entries) - 1:
                labeled["kind"] = "final"
                out.append(labeled)
        if out:
            return out
    final = dict(entries[-1])
    final["kind"] = "final"
    return [final]


def format_play_tool_result(
    result: dict[str, Any],
) -> list[dict[str, Any] | Image] | dict[str, Any]:
    """Shape an engine send_input dict into the MCP play return.

    Public JSON gets native 160x144 ``screenshots`` of the **final** LCD (plus
    an optional interrupt frame). A GIF is attached only when the engine packed
    one (``media=video``). Default observation is one native PNG of the last
    frame — never an early keyframe. Internal hashes / paths stay stripped.
    """
    pngs = _as_png_list(result.get("pngs"))
    gif = result.get("gif")
    native = _native_pngs(result)
    shot_meta = result.get("screenshots")
    mode = str(result.get("screenshot_mode") or "final")
    result.pop("pngs", None)
    result.pop("gif", None)
    result.pop("gifs", None)
    result.pop("gif_b64", None)
    result.pop("pngs_b64", None)
    result.pop("pngs_native", None)
    result.pop("pngs_native_b64", None)
    status = _public(result)
    if status.get("ok") and native:
        status["screenshots"] = _public_screenshot_list(
            native,
            shot_meta if isinstance(shot_meta, list) else None,
            mode=mode,
        )
    if not result.get("sent") and result.get("error"):
        status.pop("screenshots", None)
        return status

    image: Image | None = None
    if isinstance(gif, (bytes, bytearray)) and gif:
        image = Image(data=bytes(gif), format="gif")
    else:
        final_png = _final_image_png(pngs, native)
        if final_png:
            image = Image(data=final_png, format="png")
    if image is None:
        return status
    return [status, image]


def _unplayable_boot_error(reason: str) -> str:
    text = reason.strip()
    if text.endswith("Re-submit the complete .gb."):
        text = text[: -len("Re-submit the complete .gb.")].rstrip()
    text = text.rstrip(".")
    return f"{text}. Re-submit the complete .gb via add_rom or HTTP POST /roms."


def list_games(email: str | None = None) -> dict[str, Any]:
    bound = require_email(explicit=email)
    if isinstance(bound, dict):
        return bound
    expire_uploads()
    result = catalog_list_games(bound)
    result["ok"] = True
    return result


def boot(
    title: str | None = None,
    id: str | None = None,
    reset: bool = False,
    email: str | None = None,
) -> dict[str, Any]:
    bound = require_email(explicit=email)
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
    until_polarity: str | None = None,
    intent: str | None = None,
    media: str | None = None,
    screenshot_mode: str | None = None,
    screenshot_scale: int | None = None,
) -> list[dict[str, Any] | Image] | dict[str, Any]:
    """Press buttons on the current session and look at the returned LCD.

    Long directional ``frames`` become a hold that aborts on battle, text,
    menu, fade, or a blocked wall. Dialogue uses mash or ``intent=advance_text``.
    Default observation is one native 160x144 PNG of the last LCD. Pass
    ``media="video"`` for a GIF; ``screenshot_scale`` 2/3/4 upscales.
    """
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
    if until_polarity is not None:
        raw["until_polarity"] = until_polarity
    if intent is not None:
        raw["intent"] = intent
    if media is not None:
        raw["media"] = media
    try:
        args = parse_play_args(raw)
        play_input = play_input_from_args(args)
        if screenshot_mode is not None:
            play_input = replace(
                play_input,
                screenshot_mode=parse_screenshot_mode(screenshot_mode),
            )
        if screenshot_scale is not None:
            play_input = replace(
                play_input,
                screenshot_scale=parse_screenshot_scale(screenshot_scale),
            )
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

    return format_play_tool_result(result)


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
