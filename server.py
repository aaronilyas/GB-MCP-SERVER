#!/usr/bin/env python3
"""Game Boy MCP server entry. Implementation lives in gb_mcp.app."""

from __future__ import annotations

import argparse

import db
from gb_mcp import config
from gb_mcp.app import (
    add_rom,
    boot,
    how_to_play_resource,
    list_games,
    mcp,
    play,
    save,
    screen_resource,
    session_resource,
    stop,
)
from gb_mcp.contract import HOW_TO_PLAY
from gb_mcp.http import run_http
from gb_mcp.tools.ingest import (
    _create_isolated_container,
    _destroy_container,
    _docker_available,
    _ensure_image,
    _validate_inside_container,
    persist_mapped_rom,
    run_isolated_validation,
)

__all__ = [
    "HOW_TO_PLAY",
    "add_rom",
    "boot",
    "how_to_play_resource",
    "list_games",
    "main",
    "mcp",
    "persist_mapped_rom",
    "play",
    "run_http",
    "run_isolated_validation",
    "save",
    "screen_resource",
    "session_resource",
    "stop",
    "_create_isolated_container",
    "_destroy_container",
    "_docker_available",
    "_ensure_image",
    "_validate_inside_container",
]


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
