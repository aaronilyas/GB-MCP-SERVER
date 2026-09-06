"""Lead glue: parse payload → tick engine + vision packaging.

Imported by the play-instance loop. Must not expose emulator memory.
"""

from __future__ import annotations

import io
import time
from dataclasses import replace
from typing import Any

from PIL import Image

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

# Mash / long hold: sample internally, then pack one short GIF. Not a public mode.
_LONG_ACTION_FRAMES = 30
_GIF_MIN_MS = 1000
_GIF_MAX_MS = 3000
_GIF_TARGET_MS = 2000
_ACTION_MEDIA_KEYS = frozenset({"gif", "gifs"})


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
    public_mode = play.screenshot_mode
    play, sampled_internally = _with_internal_keyframes(play)
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
    result.setdefault("screenshot_mode", public_mode)
    if getattr(plan, "interrupt_frame_index", None) is not None:
        result.setdefault("interrupt_frame_index", plan.interrupt_frame_index)
    if play.ocr:
        result.update(_maybe_ocr(result.get("pngs") or []))
    _apply_action_media(
        result,
        want_gif=wants_action_gif(play),
        public_screenshot_mode=public_mode,
        sampled_internally=sampled_internally,
    )
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


def wants_action_gif(play: Any) -> bool:
    """True for mash / long hold that should be sampled into one GIF."""
    if getattr(play, "macro", None) not in {"mash", "hold"}:
        return False
    planned = int(getattr(play, "planned_frames", 0) or 0)
    max_frames = int(getattr(play, "max_frames", 0) or 0)
    return max(planned, max_frames) >= _LONG_ACTION_FRAMES


def pack_action_media(pngs: list[bytes], *, want_gif: bool) -> dict[str, Any]:
    """Keep the final PNG; optionally encode sampled frames as one 1–3s GIF."""
    frames = [bytes(blob) for blob in pngs if isinstance(blob, (bytes, bytearray)) and blob]
    if not frames:
        return {"pngs": []}
    final = frames[-1]
    if not want_gif or len(frames) < 2:
        return {"pngs": [final]}
    try:
        gif = _encode_gif(frames, duration_ms=_gif_frame_duration_ms(len(frames)))
    except Exception:
        return {"pngs": [final]}
    if not gif.startswith(b"GIF8"):
        return {"pngs": [final]}
    return {"pngs": [final], "gif": gif}


def _with_internal_keyframes(play: PlayInput) -> tuple[PlayInput, bool]:
    if play.screenshot_mode != "final" or not wants_action_gif(play):
        return play, False
    return replace(play, screenshot_mode="keyframes"), True


def _apply_action_media(
    result: dict[str, Any],
    *,
    want_gif: bool,
    public_screenshot_mode: str,
    sampled_internally: bool,
) -> None:
    pngs = result.get("pngs") or []
    if not isinstance(pngs, list):
        pngs = [pngs]
    if not want_gif:
        return
    media = pack_action_media(pngs, want_gif=True)
    result["pngs"] = media.get("pngs") or []
    gif = media.get("gif")
    if gif:
        result["gif"] = gif
    else:
        result.pop("gif", None)
        result.pop("gifs", None)
    result["screenshot_count"] = len(result["pngs"])
    if sampled_internally:
        result["screenshot_mode"] = public_screenshot_mode or "final"
        result["screenshots_subsampled"] = False
    shots = result.get("screenshots")
    if isinstance(shots, list) and shots and (gif or sampled_internally):
        last = dict(shots[-1])
        last["kind"] = "final"
        result["screenshots"] = [last]


def _gif_frame_duration_ms(frame_count: int) -> int:
    n = max(1, int(frame_count))
    duration = max(1, int(round(_GIF_TARGET_MS / n)))
    total = duration * n
    if total < _GIF_MIN_MS:
        duration = max(1, (_GIF_MIN_MS + n - 1) // n)
    elif total > _GIF_MAX_MS:
        duration = max(1, _GIF_MAX_MS // n)
    return duration


def _encode_gif(pngs: list[bytes], *, duration_ms: int) -> bytes:
    frames = [Image.open(io.BytesIO(blob)).convert("RGB") for blob in pngs]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=max(1, int(duration_ms)),
        loop=0,
    )
    for frame in frames:
        frame.close()
    return buf.getvalue()


def strip_forbidden_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop any key not on the send-input allowlist (defense in depth)."""
    allowed = SEND_INPUT_RESPONSE_KEYS | _ACTION_MEDIA_KEYS
    return {key: value for key, value in payload.items() if key in allowed}
