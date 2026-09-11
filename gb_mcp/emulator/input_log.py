"""Append-only JSONL diary of play inputs. Not returned over MCP."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from gb_mcp.emulator.play_limits import DEFAULT_INPUT_LOG_NAME


def frame_count(pyboy: Any) -> int:
    """Best-effort PyBoy frame index. FakePyBoy exposes ``ticks``."""
    for name in ("frame_count", "ticks"):
        value = getattr(pyboy, name, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if callable(value):
            try:
                counted = value()
            except Exception:
                continue
            if isinstance(counted, int) and not isinstance(counted, bool):
                return counted
    return 0


def resolve_log_path(pyboy: Any) -> Path | None:
    """Session ``input_log.jsonl``, or ``GB_MCP_INPUT_LOG`` if set."""
    try:
        from gb_mcp.config import input_log_path_setting

        raw = input_log_path_setting()
    except Exception:
        raw = os.environ.get("GB_MCP_INPUT_LOG", "").strip() or DEFAULT_INPUT_LOG_NAME
    path = Path(raw)
    rom = getattr(pyboy, "gamerom", None)
    rom_path = Path(str(rom)) if rom else None
    if path.is_absolute():
        return path
    if rom_path is not None:
        return rom_path.parent / path
    return path


def append_play_log(
    pyboy: Any,
    play: Any,
    result: dict[str, Any],
    *,
    t0: int,
) -> None:
    """Append one JSON line. Never raises; never mutates ``result``."""
    try:
        env_set = bool(os.environ.get("GB_MCP_INPUT_LOG", "").strip())
        rom = getattr(pyboy, "gamerom", None)
        if not env_set and rom and not Path(str(rom)).exists():
            return
        path = resolve_log_path(pyboy)
        if path is None:
            return
        record = _record(play, result, t0=int(t0), t1=frame_count(pyboy))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    except Exception:
        return


def _record(play: Any, result: dict[str, Any], *, t0: int, t1: int) -> dict[str, Any]:
    buttons = [str(name) for name in (getattr(play, "buttons", ()) or ())]
    steps = tuple(getattr(play, "steps", ()) or ())
    if not buttons and steps:
        first = steps[0]
        buttons = [str(name) for name in (getattr(first, "buttons", ()) or ())]
    until = getattr(play, "until", None)
    until_on = getattr(until, "on", None) if until is not None else None
    extra = getattr(play, "extra", None) or {}
    intent = getattr(play, "intent", None) or extra.get("intent")
    stop = result.get("stop_detail") or result.get("stop_reason")
    if not isinstance(stop, str) or not stop:
        stop = None
    speed = result.get("emulation_speed")
    if speed is None:
        speed = getattr(play, "emulation_speed", 0)
    record: dict[str, Any] = {
        "t0": int(t0),
        "t1": int(t1),
        "buttons": buttons,
        "hold_frames": int(getattr(play, "hold_frames", 0) or 0),
        "gap_frames": int(getattr(play, "gap_frames", 0) or 0),
        "macro": getattr(play, "macro", None),
        "intent": intent,
        "until": until_on,
        "stopped_reason": stop,
        "frames_ran": int(result.get("frames_advanced") or 0),
        "emulation_speed": int(speed or 0),
    }
    if len(steps) > 1:
        record["steps"] = [
            {
                "buttons": [str(name) for name in (getattr(step, "buttons", ()) or ())],
                "hold_frames": int(getattr(step, "hold_frames", 0) or 0),
                "gap_frames": int(getattr(step, "gap_frames", 0) or 0),
            }
            for step in steps
        ]
    return record
