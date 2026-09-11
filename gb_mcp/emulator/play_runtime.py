"""Lead glue: parse payload → tick engine + vision packaging.

Imported by the play-instance loop. Must not expose emulator memory.
"""

from __future__ import annotations

import io
import time
from dataclasses import replace
from typing import Any

from PIL import Image

from gb_mcp.emulator.input_schema import InputStep, PlayInput, UntilSpec, parse_play_input
from gb_mcp.emulator.input_engine import run_play_input
from gb_mcp.emulator.play_limits import (
    DEFAULT_HASH_REGIONS,
    DEFAULT_REGION,
    DEFAULT_STABLE_FRAMES,
    LONG_ACTION_FRAMES,
    NATIVE_SIZE,
    PUBLIC_MASH_ABORT_GAP_FRAMES,
    SEND_INPUT_RESPONSE_KEYS,
)
from gb_mcp.emulator.vision import (
    ScreenshotPlan,
    UntilMonitor,
    capture_native,
    classify,
    hash_named_regions,
    player_moved_from_frames,
    textbox_complete,
)

# Mash / long hold: sample internally, then pack one short GIF. Not a public mode.
_LONG_ACTION_FRAMES = LONG_ACTION_FRAMES
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
    pack_media: bool = True,
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
        extra = dict(play.extra or {})
        media_arg = raw.get("media")
        if isinstance(media_arg, str) and media_arg.strip():
            extra.setdefault("media", media_arg.strip().lower())
        if raw.get("public_mash"):
            extra["public_mash"] = True
        if extra:
            play = replace(play, extra=extra)
    from gb_mcp.emulator.input_log import append_play_log, frame_count

    t0 = frame_count(pyboy)
    if play.intent:
        from gb_mcp.emulator.intents import execute_intent

        result = execute_intent(
            pyboy, play, session_speed=session_speed, monotonic=monotonic
        )
        if pack_media:
            append_play_log(pyboy, play, result, t0=t0)
        return result
    play = _rewrite_public_mash_without_box(pyboy, play)
    public_mode = play.screenshot_mode
    media = str((play.extra or {}).get("media") or "image")
    play, sampled_internally = _with_internal_keyframes(play)
    baseline = capture_native(pyboy)
    monitor = UntilMonitor(play, baseline)
    plan = ScreenshotPlan(play)
    if wants_action_gif(play):
        plan.record(0, baseline, interrupt=False, final=False)
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
    flags = classify(final_frame)
    result.setdefault("classifiers", flags)
    result.setdefault("screenshot_scale", play.screenshot_scale)
    result.setdefault("native_size", list(NATIVE_SIZE))
    result.setdefault("emulation_speed", play.emulation_speed)
    result.setdefault("macro", play.macro)
    result.setdefault("until_eval_interval", play.until_eval_interval)
    result.setdefault("default_hold_abort_applied", play.apply_default_hold_abort)
    result.setdefault("gap_frames", play.gap_frames)
    result.setdefault("screenshot_mode", public_mode)
    moved = player_moved_from_frames(baseline, final_frame)
    if result.get("stop_detail") == "blocked" or result.get("stop_reason") == "blocked":
        moved = False
    result["player_moved"] = moved
    if flags.get("textbox_likely"):
        result["textbox_complete"] = textbox_complete(final_frame)
    if getattr(plan, "interrupt_frame_index", None) is not None:
        result.setdefault("interrupt_frame_index", plan.interrupt_frame_index)
    if play.ocr:
        result.update(_maybe_ocr(result.get("pngs") or []))
    elif flags.get("textbox_likely"):
        _attach_public_ocr(result)
    if pack_media:
        _apply_action_media(
            result,
            want_gif=wants_action_gif(play),
            public_screenshot_mode=public_mode,
            sampled_internally=sampled_internally,
            media=media,
        )
        cleaned = strip_forbidden_keys(result)
        append_play_log(pyboy, play, cleaned, t0=t0)
        return cleaned
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


def _attach_public_ocr(result: dict[str, Any]) -> None:
    """OCR the inner textbox of the last native PNG. Never fail the play call."""
    natives = result.get("pngs_native") or result.get("pngs") or []
    if not isinstance(natives, list) or not natives:
        return
    blob = natives[-1]
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return
    try:
        from gb_mcp.emulator.ocr import ocr_textbox_png
    except Exception:
        return
    try:
        text = ocr_textbox_png(bytes(blob))
    except Exception:
        return
    if text:
        result["ocr_text"] = text


def _is_public_mash(play: PlayInput) -> bool:
    extra = play.extra or {}
    return bool(extra.get("public_mash"))


def _rewrite_public_mash_without_box(pyboy: Any, play: PlayInput) -> PlayInput:
    """Public mash must not hold A. No textbox → brief wait instead of hundreds of frames."""
    if play.macro != "mash" or not _is_public_mash(play):
        return play
    flags = classify(capture_native(pyboy))
    if flags.get("textbox_likely"):
        until = play.until
        if until is None:
            play = replace(
                play,
                until=UntilSpec(
                    region=DEFAULT_REGION,
                    on="classifier",
                    threshold=0.08,
                    stable_frames=DEFAULT_STABLE_FRAMES,
                    classifier="textbox_likely",
                    classifier_polarity="disappears",
                ),
            )
        return play
    wait = PUBLIC_MASH_ABORT_GAP_FRAMES
    return replace(
        play,
        macro="steps",
        steps=(InputStep(buttons=(), hold_frames=wait, gap_frames=0, wait=True),),
        buttons=(),
        max_frames=wait,
        planned_frames=wait,
        hold_frames=wait,
        until=None,
        apply_default_hold_abort=False,
        disable_default_hold_abort=True,
        intent=None,
    )


def wants_action_gif(play: Any) -> bool:
    """True only when the caller opts in with media=video on a long mash/hold."""
    extra = getattr(play, "extra", None) or {}
    media = str(extra.get("media") or "image").strip().lower()
    if media != "video":
        return False
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
    # Sample mash/hold internally so a GIF can be packed even if the public
    # screenshot_mode was "final" (media=video) or the hold aborts early.
    if not wants_action_gif(play):
        return play, False
    if play.screenshot_mode == "keyframes":
        return play, False
    return replace(play, screenshot_mode="keyframes"), True


def _apply_action_media(
    result: dict[str, Any],
    *,
    want_gif: bool,
    public_screenshot_mode: str,
    sampled_internally: bool,
    media: str = "image",
) -> None:
    pngs = result.get("pngs") or []
    if not isinstance(pngs, list):
        pngs = [pngs]
    emit_gif = media == "video" and bool(want_gif)
    keep_natives = public_screenshot_mode in {"keyframes", "all"}
    if not emit_gif:
        if keep_natives:
            return
        pngs = result.get("pngs") or []
        if isinstance(pngs, list) and pngs:
            result["pngs"] = pngs[-1:]
            result["screenshot_count"] = 1
        native = result.get("pngs_native")
        if isinstance(native, list) and native:
            last_native = native[-1]
            if isinstance(last_native, (bytes, bytearray)) and last_native:
                result["pngs_native"] = [bytes(last_native)]
            else:
                result["pngs_native"] = native[-1:]
        shots = result.get("screenshots")
        if isinstance(shots, list) and shots:
            last = dict(shots[-1])
            last["kind"] = "final"
            result["screenshots"] = [last]
        result["screenshot_mode"] = public_screenshot_mode or "final"
        return
    packed = pack_action_media(pngs, want_gif=True)
    gif = packed.get("gif")
    if gif:
        result["gif"] = gif
    else:
        result.pop("gif", None)
        result.pop("gifs", None)
    if keep_natives:
        result["screenshot_count"] = len(result.get("pngs_native") or result.get("pngs") or [])
        return
    result["pngs"] = packed.get("pngs") or []
    result["screenshot_count"] = len(result["pngs"])
    if sampled_internally:
        result["screenshot_mode"] = public_screenshot_mode or "final"
        result["screenshots_subsampled"] = False
    collapse = bool(gif or sampled_internally)
    shots = result.get("screenshots")
    if isinstance(shots, list) and shots and collapse:
        last = dict(shots[-1])
        last["kind"] = "final"
        result["screenshots"] = [last]
    native = result.get("pngs_native")
    if collapse and isinstance(native, list) and native:
        last_native = native[-1]
        if isinstance(last_native, (bytes, bytearray)) and last_native:
            result["pngs_native"] = [bytes(last_native)]
        else:
            result["pngs_native"] = native[-1:]


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
