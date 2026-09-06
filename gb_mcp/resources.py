"""Model-facing MCP resources. No per-user URI templates."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.utilities.types import Image

from gb_mcp.contract import HOW_TO_PLAY
from gb_mcp.emulator import session as pyboy_sessions
from gb_mcp.emulator.loop import shape_public_status
from gb_mcp.identity import require_email
from gb_mcp.tools.play import capture_screen_png


def how_to_play() -> str:
    return HOW_TO_PLAY


def screen() -> Image | dict[str, Any]:
    captured = capture_screen_png()
    if isinstance(captured, dict):
        return captured
    return Image(data=captured, format="png")


def session() -> dict[str, Any]:
    bound = require_email()
    if isinstance(bound, dict):
        return bound
    current = pyboy_sessions.manager.current(bound)
    if isinstance(current, dict):
        return shape_public_status(current)
    return shape_public_status(current.status())
