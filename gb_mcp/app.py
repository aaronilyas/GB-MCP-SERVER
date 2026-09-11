"""MCP server entry: six play tools and three resources."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from pydantic import Field

from gb_mcp.contract import HOW_TO_PLAY
from gb_mcp.http import attach_public_routes
from gb_mcp.resources import how_to_play as how_to_play_body
from gb_mcp.resources import screen as screen_body
from gb_mcp.resources import session as session_body
from gb_mcp.tools import ingest, play as play_tools

mcp = MCPServer("gb-mcp-server", instructions=HOW_TO_PLAY)

_EMAIL_DESCRIPTION = (
    "Email of the LLM user (application identity, not transport auth). "
    "Optional when the current OAuth access token has an email or email-shaped "
    "sub claim. An explicit email wins. If omitted and there is no token "
    "identity, the result includes model_request asking for email. Ask the user "
    "if you do not already have it. Do not invent an email."
)

_IDENTITY_EMAIL_TOOLS = ("list_games", "boot", "add_rom")
_SESSION_TOOLS = ("play", "save", "stop")


def _email_input_schema() -> dict[str, Any]:
    return {"type": "string", "description": _EMAIL_DESCRIPTION}


def _passthrough_explicit_email(tool: Any) -> None:
    """Keep JSON-RPC ``email`` even if the generated arg model omitted it."""
    metadata = tool.fn_metadata
    original = metadata.validate_arguments

    def validate_arguments(arguments_to_validate: dict[str, Any]) -> dict[str, Any]:
        payload = arguments_to_validate if isinstance(arguments_to_validate, dict) else {}
        explicit = payload.get("email") if isinstance(payload, dict) else None
        kwargs = original(arguments_to_validate)
        if isinstance(explicit, str) and explicit.strip():
            kwargs["email"] = explicit
        return kwargs

    object.__setattr__(metadata, "validate_arguments", validate_arguments)


def publish_identity_email_schemas() -> None:
    """Advertise optional ``email`` on list/boot/add_rom and keep extras.

    Hosted connector catalogs copy ``tools/list``. A missing ``email`` field
    plus ``additionalProperties: false`` strips the argument before
    ``require_email(explicit=email)``. Session tools stay email-less.
    """
    email_schema = _email_input_schema()
    for name in _IDENTITY_EMAIL_TOOLS:
        tool = mcp._tool_manager.get_tool(name)
        if tool is None:
            continue
        schema = tool.parameters
        properties = schema.setdefault("properties", {})
        properties["email"] = dict(email_schema)
        required = [item for item in (schema.get("required") or []) if item != "email"]
        if required:
            schema["required"] = required
        else:
            schema.pop("required", None)
        schema["type"] = "object"
        schema["additionalProperties"] = True
        _passthrough_explicit_email(tool)
    for name in _SESSION_TOOLS:
        tool = mcp._tool_manager.get_tool(name)
        if tool is None:
            continue
        properties = (tool.parameters or {}).get("properties") or {}
        properties.pop("email", None)


@mcp.tool(
    name="add_rom",
    description=(
        "Submit a small Game Boy / Game Boy Color homebrew ROM as one base64 "
        "argument. Isolated Docker validation runs with no internet; on success "
        "the file is saved and mapped from the current session identity. Use "
        "HTTP POST /roms for dumps too large for one chat argument."
    ),
)
def add_rom(
    rom_base64: Annotated[
        str,
        Field(description="Base64-encoded .gb/.gbc bytes for a small homebrew ROM."),
    ],
    filename: Annotated[
        str,
        Field(default="rom.gb", description="Preferred filename if the ROM is accepted."),
    ] = "rom.gb",
    email: Annotated[
        str | None,
        Field(default=None, description=_EMAIL_DESCRIPTION),
    ] = None,
) -> dict[str, Any]:
    return ingest.add_rom(rom_base64, filename=filename, email=email)


@mcp.tool(
    name="list_games",
    description="List this user's games as title, id, and playable.",
)
def list_games(
    email: Annotated[
        str | None,
        Field(default=None, description=_EMAIL_DESCRIPTION),
    ] = None,
) -> dict[str, Any]:
    return play_tools.list_games(email=email)


@mcp.tool(
    name="boot",
    description=(
        "Start or resume a session by cartridge title or id. Default restores "
        "the last snapshot. reset=true cold-boots and drops the snapshot."
    ),
)
def boot(
    title: Annotated[
        str | None,
        Field(default=None, description="Cartridge title (case-insensitive)."),
    ] = None,
    id: Annotated[
        str | None,
        Field(default=None, description="32-hex game id from list_games."),
    ] = None,
    reset: Annotated[
        bool,
        Field(
            default=False,
            description="If true, drop the snapshot and cold-boot.",
        ),
    ] = False,
    email: Annotated[
        str | None,
        Field(default=None, description=_EMAIL_DESCRIPTION),
    ] = None,
) -> dict[str, Any]:
    return play_tools.boot(title=title, id=id, reset=reset, email=email)


@mcp.tool(
    name="play",
    description=(
        "Press Game Boy buttons and look at the returned image (native 160x144 "
        "PNG of the last LCD). After boot, do not pass email or id. Omit frames "
        "on a single D-pad to hold-walk (default 1800); pass frames=16 to tap. "
        "Holds abort on combat HUD, text, menu, fade, or blocked. buttons=[] "
        "waits. Optional frames, gap, mash, steps, until "
        "(battle|textbox|menu|stable|fade|blocked|overworld), until_polarity "
        "(appears|disappears), intent "
        "(advance_text|run_away|battle_turn|battle_until_overworld|skip_intro|"
        "enter_door), media (off|image|video; do not request GIFs unless video), "
        "screenshot_mode, and screenshot_scale (1–4, default 1)."
    ),
)
def play(
    buttons: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Buttons pressed together: a, b, start, select, up, down, left, "
                "right. Empty list waits. Omit when passing steps, mash, or intent."
            ),
        ),
    ] = None,
    frames: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Hold or wait frames. Omit on a single D-pad to walk (default 1800 "
                "hold). Pass frames=16 to tap one tile. A/B default 16. Holds abort "
                "on combat HUD, text, menu, fade, or blocked."
            ),
        ),
    ] = None,
    gap: Annotated[
        int | None,
        Field(default=None, description="Released frames after the chord."),
    ] = None,
    mash: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "If true, pulse A until the textbox is gone, then release. "
                "Use for dialogue. Do not hold A."
            ),
        ),
    ] = None,
    steps: Annotated[
        list[dict[str, Any]] | None,
        Field(
            default=None,
            description="Ordered chords. Do not pass with top-level buttons.",
        ),
    ] = None,
    until: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Stop early on battle, textbox, menu, stable, fade, blocked, or "
                "overworld (HUD gone on a non-uniform field). Aliases: textbox_end, "
                "clear_text. After a door prefer fade-disappears / overworld, not "
                "stable-on-black."
            ),
        ),
    ] = None,
    until_polarity: Annotated[
        str | None,
        Field(
            default=None,
            description="appears (default) or disappears. Use disappears to mash a textbox until it is gone.",
        ),
    ] = None,
    intent: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "advance_text, run_away, battle_turn, battle_until_overworld, "
                "skip_intro, or enter_door. Composed from buttons and until; "
                "LCD-driven, not memory."
            ),
        ),
    ] = None,
    media: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "off, image, or video. Default image: one native PNG of the last "
                "LCD, no GIF. media=video opts in to a GIF. Do not request GIFs "
                "or scale-4 shots for ordinary play."
            ),
        ),
    ] = None,
    screenshot_mode: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "final (default), interrupt_and_final, keyframes, or all. "
                "Omit for one last-frame PNG."
            ),
        ),
    ] = None,
    screenshot_scale: Annotated[
        int | None,
        Field(
            default=None,
            description="PNG upscale 1, 2, 3, or 4. Default 1 (native 160x144).",
        ),
    ] = None,
) -> list[dict[str, Any] | Image] | dict[str, Any]:
    return play_tools.play(
        buttons=buttons,
        frames=frames,
        gap=gap,
        mash=mash,
        steps=steps,
        until=until,
        until_polarity=until_polarity,
        intent=intent,
        media=media,
        screenshot_mode=screenshot_mode,
        screenshot_scale=screenshot_scale,
    )


@mcp.tool(
    name="save",
    description="Write a snapshot and leave the session running.",
)
def save() -> dict[str, Any]:
    return play_tools.save()


@mcp.tool(
    name="stop",
    description="Save, then close the current session.",
)
def stop() -> dict[str, Any]:
    return play_tools.stop()


@mcp.resource(
    "gb://how-to-play",
    mime_type="text/markdown",
    description="How a connected model should play. Same text as server instructions.",
)
def how_to_play_resource() -> str:
    return how_to_play_body()


@mcp.resource(
    "gb://screen",
    mime_type="image/png",
    description="Current session screen as one PNG, when a game is running.",
)
def screen_resource() -> Image | dict[str, Any]:
    return screen_body()


@mcp.resource(
    "gb://session",
    mime_type="application/json",
    description="Current session public status (ok, frames, stopped, game).",
)
def session_resource() -> dict[str, Any]:
    return session_body()


publish_identity_email_schemas()
attach_public_routes(mcp)
