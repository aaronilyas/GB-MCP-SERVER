"""Lead glue: parse payload → tick engine + vision packaging.

Imported by the play-instance loop. Must not expose emulator memory.
"""

from __future__ import annotations

import time
from typing import Any

from gb_mcp.emulator.input_schema import PlayInput, parse_play_input
from gb_mcp.emulator.input_engine import run_play_input
from gb_mcp.emulator.play_limits import (
    DEFAULT_HASH_REGIONS,
    NATIVE_SIZE,
    SEND_INPUT_RESPONSE_KEYS,
)
from gb_mcp.emulator.vision import (
    ScreenshotPlan,
    UntilMonitor,
    capture_native,
    classify,
    hash_named_regions,
)


def execute_play_command(
    pyboy: Any,
    payload: dict[str, Any] | PlayInput,
    *,
    session_speed: int | None = None,
    monotonic=time.monotonic,
) -> dict[str, Any]:
    """Run one send_pyboy_input payload against a live PyBoy-like object."""
    if isinstance(payload, PlayInput):
        play = payload
    else:
        raw = dict(payload)
        if not raw.get("steps"):
            raw.pop("steps", None)
        elif raw.get("buttons"):
            # asdict(PlayInput) for a buttons chord includes both; steps win.
            raw.pop("buttons", None)
        play = parse_play_input(raw, session_speed=session_speed)
    baseline = capture_native(pyboy)
    monitor = UntilMonitor(play, baseline)
    plan = ScreenshotPlan(play)
    result = run_play_input(
        pyboy,
        play,
        capture_native=lambda: capture_native(pyboy),
        until_monitor=monitor,
        screenshot_plan=plan,
        monotonic=monotonic,
    )
    packed = plan.package(play) if "pngs" not in result else {}
    if packed:
        for key, value in packed.items():
            result.setdefault(key, value)
    # Guarantee contract metadata even if a stub plan omitted it.
    final_frame = getattr(plan, "final_frame", None)
    if final_frame is None:
        final_frame = baseline
    result.setdefault("region_hashes", hash_named_regions(final_frame, play.hash_regions or DEFAULT_HASH_REGIONS))
    result.setdefault("classifiers", classify(final_frame))
    result.setdefault("screenshot_scale", play.screenshot_scale)
    result.setdefault("native_size", list(NATIVE_SIZE))
    result.setdefault("emulation_speed", play.emulation_speed)
    result.setdefault("macro", play.macro)
    result.setdefault("until_eval_interval", play.until_eval_interval)
    result.setdefault("default_hold_abort_applied", play.apply_default_hold_abort)
    result.setdefault("gap_frames", play.gap_frames)
    result.setdefault("screenshot_mode", play.screenshot_mode)
    if getattr(plan, "interrupt_frame_index", None) is not None:
        result.setdefault("interrupt_frame_index", plan.interrupt_frame_index)
    if play.ocr:
        result.update(_maybe_ocr(result.get("pngs") or []))
    return strip_forbidden_keys(result)


def _maybe_ocr(pngs: list[bytes]) -> dict[str, Any]:
    try:
        from gb_mcp.emulator.ocr import ocr_pngs
    except Exception:
        return {"ocr_text": None, "ocr_engine": None, "ocr_error": "disabled"}
    try:
        return ocr_pngs(pngs)
    except Exception as exc:  # noqa: BLE001
        return {"ocr_text": None, "ocr_engine": None, "ocr_error": str(exc) or "disabled"}


def strip_forbidden_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop any key not on the send-input allowlist (defense in depth)."""
    return {key: value for key, value in payload.items() if key in SEND_INPUT_RESPONSE_KEYS}
