"""List mapped ROM games for an email without MCP registration."""

from __future__ import annotations

from typing import Any

import db
from gb_mcp.storage.roms import _describe_subdirectory


def list_games(email: str) -> dict[str, Any]:
    """Return games mapped to ``email`` as ``{games: [{title, id, playable}, ...]}``.

    ``id`` is the 32-hex subdirectory mapping name. Unknown emails yield an
    empty list; this does not create a user.
    """
    with db.session_scope() as session:
        rows = db.list_subdirectories_for_email(session, email)
        mapped = [(row.name, row.created_at) for row in rows]

    games: list[dict[str, Any]] = []
    for name, created_at in mapped:
        info = _describe_subdirectory(name, created_at)
        described = info.get("games") or []
        if not described:
            games.append({"title": None, "id": name, "playable": False})
            continue
        for game in described:
            games.append(
                {
                    "title": game.get("title"),
                    "id": name,
                    "playable": bool(game.get("playable", False)),
                }
            )
    return {"games": games}
