"""Allocate unique roms/ subdirectories and catalog stored files."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from gb_mcp import config
from gb_mcp.gb.constants import ROM_SUFFIXES
from gb_mcp.gb.header import _read_rom_identity, assert_rom_playable


def _sanitize_filename(name: str) -> str:
    base = Path(name).name.strip() or "rom.gb"
    base = re.sub(r"[^\w.\-]+", "_", base)
    if not base.lower().endswith((".gb", ".gbc")):
        base = f"{base}.gb"
    return base[:180]


def _allocate_subdirectory_name() -> str:
    """Pick a 32-character name that is unused on disk and in the mapping DB."""
    config.ROMS_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        name = db.new_subdirectory_name()
        if (config.ROMS_DIR / name).exists():
            continue
        with db.session_scope() as session:
            taken = db.subdirectory_exists(session, name)
        if not taken:
            return name
    raise RuntimeError("failed to allocate a unique 32-character subdirectory name")


def _persist_validated_rom(
    subdirectory: str,
    safe_name: str,
    rom_bytes: bytes,
    *,
    replace: bool = False,
) -> Path:
    dest_dir = config.ROMS_DIR / subdirectory
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    if not replace and dest.exists():
        dest = dest_dir / f"{dest.stem}-{uuid.uuid4().hex[:8]}{dest.suffix}"

    # Persist only after in-container validation succeeded.
    # Write via a same-directory temp file then replace for atomicity.
    fd, tmp_name = tempfile.mkstemp(prefix=".rom-", suffix=".tmp", dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(rom_bytes)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    if replace:
        dest_resolved = dest.resolve()
        for path in _iter_subdirectory_files(dest_dir):
            if path.suffix.lower() not in ROM_SUFFIXES:
                continue
            try:
                if path.resolve() == dest_resolved:
                    continue
            except OSError:
                continue
            try:
                path.unlink()
            except OSError:
                pass
    return dest


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _state_path_for_rom(rom_path: Path) -> Path:
    """Return the PyBoy save-state path stored next to the ROM (`rom.gb.state`)."""
    return Path(str(rom_path) + ".state")


def _rom_in_subdirectory(name: str) -> Path:
    """Return the first valid Game Boy ROM in `roms/<name>/`.

    Raises:
        FileNotFoundError: If the subdirectory is missing or has no usable ROM.
    """
    dest = config.ROMS_DIR / name
    if not dest.is_dir():
        raise FileNotFoundError(f"subdirectory {name!r} does not exist under roms/")

    roms = [path for path in _iter_subdirectory_files(dest) if path.suffix.lower() in ROM_SUFFIXES]
    if not roms:
        raise FileNotFoundError(f"no Game Boy ROM found in subdirectory {name!r}")

    last_unplayable: ValueError | None = None
    for path in roms:
        identity = _read_rom_identity(path)
        if "error" in identity:
            continue
        try:
            assert_rom_playable(path)
        except ValueError as exc:
            last_unplayable = exc
            continue
        return path
    if last_unplayable is not None:
        raise last_unplayable
    raise FileNotFoundError(f"no valid Game Boy ROM found in subdirectory {name!r}")


def _iter_subdirectory_files(dest: Path) -> list[Path]:
    dest_resolved = dest.resolve()
    files: list[Path] = []
    for path in dest.iterdir():
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved.name.startswith("."):
            continue
        if not resolved.is_relative_to(dest_resolved):
            continue
        files.append(resolved)
    files.sort(key=lambda p: p.name.lower())
    return files


def _describe_subdirectory(name: str, created_at: datetime | None) -> dict[str, Any]:
    dest = config.ROMS_DIR / name
    info: dict[str, Any] = {
        "subdirectory": name,
        "path": f"roms/{name}",
        "created_at": _isoformat(created_at),
        "exists_on_disk": False,
        "files": [],
        "games": [],
    }
    try:
        info["exists_on_disk"] = dest.is_dir()
        if not info["exists_on_disk"]:
            info["summary"] = "mapped in the database but missing from disk"
            return info

        files: list[dict[str, Any]] = []
        games: list[dict[str, Any]] = []
        for path in _iter_subdirectory_files(dest):
            stat = path.stat()
            entry: dict[str, Any] = {
                "filename": path.name,
                "path": str(path.relative_to(config.ROOT)),
                "size_bytes": stat.st_size,
                "modified_at": _isoformat(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
            }
            if path.suffix.lower() in ROM_SUFFIXES:
                identity = _read_rom_identity(path)
                entry.update(identity)
                # Header-unreadable .gb/.gbc stay in files, not in games.
                # Header-parseable but truncated dumps stay in games with playable: false.
                if "error" not in identity:
                    game: dict[str, Any] = {
                        "title": identity.get("title"),
                        "filename": path.name,
                        "platform": identity.get("platform"),
                        "cartridge_type": identity.get("cartridge_type"),
                        "has_battery": identity.get("has_battery"),
                        "size_bytes": stat.st_size,
                        "playable": bool(identity.get("playable", True)),
                    }
                    if not game["playable"] and identity.get("unplayable_reason"):
                        game["unplayable_reason"] = identity["unplayable_reason"]
                    games.append(game)
            else:
                entry["kind"] = "other"
            files.append(entry)

        info["files"] = files
        info["games"] = games
        titles = [g["title"] for g in games if g.get("title")]
        if not files:
            info["summary"] = "empty subdirectory"
        elif titles:
            unique = list(dict.fromkeys(titles))
            n = len(files)
            info["summary"] = f"{', '.join(unique)} ({n} file{'s' if n != 1 else ''})"
        else:
            info["summary"] = ", ".join(f["filename"] for f in files)
        return info
    except (OSError, ValueError) as exc:
        info["error"] = str(exc)
        info["summary"] = "could not be read"
        return info
