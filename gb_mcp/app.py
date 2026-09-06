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
) -> dict[str, Any]:
    return ingest.add_rom(rom_base64, filename=filename)


@mcp.tool(
    name="list_games",
    description="List this user's games as title, id, and playable.",
)
def list_games() -> dict[str, Any]:
    return play_tools.list_games()


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
) -> dict[str, Any]:
    return play_tools.boot(title=title, id=id, reset=reset)


@mcp.tool(
    name="play",
    description=(
        "Press Game Boy buttons on the current session and look at the returned "
        "PNG or short GIF. After boot, do not pass email or id. buttons=[] waits. "
        "Optional frames, gap, mash, steps, until (battle|textbox|menu|stable|fade), "
        "and media (image|video)."
    ),
)
def play(
    buttons: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Buttons pressed together: a, b, start, select, up, down, left, "
                "right. Empty list waits. Omit when passing steps or mash."
            ),
        ),
    ] = None,
    frames: Annotated[
        int | None,
        Field(default=None, description="Hold or wait frames. Default 16."),
    ] = None,
    gap: Annotated[
        int | None,
        Field(default=None, description="Released frames after the chord."),
    ] = None,
    mash: Annotated[
        bool | None,
        Field(default=None, description="If true, mash A for frames ticks."),
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
            description="Stop early on battle, textbox, menu, stable, or fade.",
        ),
    ] = None,
    media: Annotated[
        str | None,
        Field(default=None, description="image (default PNG) or video (GIF when available)."),
    ] = None,
) -> list[dict[str, Any] | Image] | dict[str, Any]:
    return play_tools.play(
        buttons=buttons,
        frames=frames,
        gap=gap,
        mash=mash,
        steps=steps,
        until=until,
        media=media,
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


attach_public_routes(mcp)
