"""Pure-Python request normalization for send_pyboy_input.

No numpy, no PyBoy. Safe to import from the MCP host image.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gb_mcp.emulator.play_limits import (
    BOTTOM_REGION,
    BUTTONS,
    CENTER_REGION,
    CLASSIFIER_POLARITIES,
    CLASSIFIERS,
    DEFAULT_CALL_TIMEOUT_SECONDS,
    DEFAULT_EMULATION_SPEED,
    DEFAULT_GAP_FRAMES,
    DEFAULT_HASH_REGIONS,
    DEFAULT_HOLD_ABORT_LUMA_JUMP,
    DEFAULT_HOLD_ABORT_THRESHOLD,
    DEFAULT_MASH_BUTTON,
    DEFAULT_MASH_PRESS_FRAMES,
    DEFAULT_MASH_RELEASE_FRAMES,
    DEFAULT_REGION,
    DEFAULT_SCREENSHOT_MODE,
    DEFAULT_SCREENSHOT_SCALE,
    DEFAULT_STABLE_FRAMES,
    DEFAULT_UNTIL_EVAL_INTERVAL,
    DEFAULT_UNTIL_THRESHOLD,
    EMULATION_SPEEDS,
    MACROS,
    MAX_CALL_TIMEOUT_SECONDS,
    MAX_FRAMES_PER_CALL,
    MAX_GAP_FRAMES,
    MAX_HOLD_FRAMES,
    MAX_INPUT_STEPS,
    MAX_UNTIL_EVAL_INTERVAL,
    MIN_UNTIL_EVAL_INTERVAL,
    NATIVE_HEIGHT,
    NATIVE_WIDTH,
    SCREENSHOT_MODES,
    SCREENSHOT_SCALES,
    UNTIL_ONS,
)


@dataclass(frozen=True)
class UntilSpec:
    """Framebuffer interrupt. Coordinates are native 160x144."""

    region: tuple[int, int, int, int]
    on: str
    threshold: float = DEFAULT_UNTIL_THRESHOLD
    stable_frames: int = DEFAULT_STABLE_FRAMES
    hash: str | None = None
    classifier: str | None = None
    classifier_polarity: str = "appears"


@dataclass(frozen=True)
class InputStep:
    buttons: tuple[str, ...]
    hold_frames: int
    gap_frames: int
    wait: bool = False


@dataclass(frozen=True)
class PlayInput:
    """Normalized send_pyboy_input body consumed by the tick engine."""

    macro: str
    steps: tuple[InputStep, ...]
    buttons: tuple[str, ...]
    hold_frames: int
    mash_button: str
    mash_press_frames: int
    mash_release_frames: int
    max_frames: int
    gap_frames: int
    emulation_speed: int
    screenshot_mode: str
    screenshot_scale: int
    until: UntilSpec | None
    until_eval_interval: int
    disable_default_hold_abort: bool
    # Two-gate default abort for macro=hold (force-off via this flag or until.on=none):
    # full-frame pixel_delta > default_hold_abort_threshold (0.12) AND
    # (battle_likely or start_menu_likely became true vs start-of-call, or mean
    # luminance jumped by more than DEFAULT_HOLD_ABORT_LUMA_JUMP). Camera scroll
    # and 1–3 tile walks do not abort; battle takeover, start menu, and warp fade do.
    apply_default_hold_abort: bool
    default_hold_abort_threshold: float
    default_hold_abort_luma_jump: float
    hash_regions: dict[str, tuple[int, int, int, int]]
    ocr: bool
    call_timeout_seconds: float
    planned_frames: int
    extra: dict[str, Any] = field(default_factory=dict)


def parse_emulation_speed(value: Any, *, default: int = DEFAULT_EMULATION_SPEED) -> int:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"uncapped", "unlimited", "max"}:
            return 0
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            value = int(text)
        else:
            raise ValueError(
                "emulation_speed must be 0 (uncapped), 1, 2, 4, 8, or 'uncapped'"
            )
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "emulation_speed must be 0 (uncapped), 1, 2, 4, 8, or 'uncapped'"
        )
    if value not in EMULATION_SPEEDS:
        raise ValueError(
            "emulation_speed must be 0 (uncapped), 1, 2, 4, 8, or 'uncapped'"
        )
    return value


def parse_screenshot_mode(value: Any) -> str:
    if value is None:
        return DEFAULT_SCREENSHOT_MODE
    if not isinstance(value, str):
        raise ValueError(
            "screenshot_mode must be 'final', 'all', 'interrupt_and_final', or 'keyframes'"
        )
    mode = value.strip().lower()
    if mode not in SCREENSHOT_MODES:
        raise ValueError(
            "screenshot_mode must be 'final', 'all', 'interrupt_and_final', or 'keyframes'"
        )
    return mode


def parse_screenshot_scale(value: Any) -> int:
    if value is None:
        return DEFAULT_SCREENSHOT_SCALE
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("screenshot_scale must be 1, 2, 3, or 4")
    if value not in SCREENSHOT_SCALES:
        raise ValueError("screenshot_scale must be 1, 2, 3, or 4")
    return value


def _normalize_button(button: Any) -> str:
    if not isinstance(button, str):
        raise ValueError(
            f"invalid button {button!r}; expected one of {', '.join(sorted(BUTTONS))}"
        )
    value = button.strip().lower()
    if value not in BUTTONS:
        raise ValueError(
            f"invalid button {button!r}; expected one of {', '.join(sorted(BUTTONS))}"
        )
    return value


def normalize_buttons(buttons: Any, *, allow_empty: bool) -> list[str]:
    if buttons is None:
        if allow_empty:
            return []
        raise ValueError("at least one button is required")
    if not isinstance(buttons, list):
        raise ValueError("buttons must be a list of Game Boy button names")
    if not buttons:
        if allow_empty:
            return []
        raise ValueError("at least one button is required")
    normalized: list[str] = []
    seen: set[str] = set()
    for button in buttons:
        value = _normalize_button(button)
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def normalize_hold_frames(value: Any, *, default: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"hold_frames must be an integer from 1 to {MAX_HOLD_FRAMES}")
    if value < 1 or value > MAX_HOLD_FRAMES:
        raise ValueError(f"hold_frames must be an integer from 1 to {MAX_HOLD_FRAMES}")
    return value


def normalize_gap_frames(value: Any, *, default: int = DEFAULT_GAP_FRAMES) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"gap_frames must be an integer from 0 to {MAX_GAP_FRAMES}")
    if value < 0 or value > MAX_GAP_FRAMES:
        raise ValueError(f"gap_frames must be an integer from 0 to {MAX_GAP_FRAMES}")
    return value


def normalize_max_frames(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"max_frames must be an integer from 1 to {MAX_FRAMES_PER_CALL}"
        )
    if value < 1 or value > MAX_FRAMES_PER_CALL:
        raise ValueError(
            f"max_frames must be an integer from 1 to {MAX_FRAMES_PER_CALL}"
        )
    return value


def normalize_region(value: Any, *, default: tuple[int, int, int, int] = DEFAULT_REGION) -> tuple[int, int, int, int]:
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("region must be [x, y, w, h] in native 160x144 space")
    try:
        x, y, w, h = (int(part) for part in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("region must be [x, y, w, h] in native 160x144 space") from exc
    if w <= 0 or h <= 0:
        raise ValueError("region width and height must be positive")
    if x < 0 or y < 0 or x + w > NATIVE_WIDTH or y + h > NATIVE_HEIGHT:
        raise ValueError("region must lie entirely inside the native 160x144 screen")
    return (x, y, w, h)


def parse_until(value: Any) -> UntilSpec | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("until must be an object")
    on_raw = value.get("on", "pixel_delta_above")
    if not isinstance(on_raw, str):
        raise ValueError(
            "until.on must be one of: pixel_delta_above, pixel_delta_below, "
            "stable, region_hash_eq, region_hash_neq, classifier, none"
        )
    on = on_raw.strip().lower()
    if on not in UNTIL_ONS:
        raise ValueError(
            "until.on must be one of: pixel_delta_above, pixel_delta_below, "
            "stable, region_hash_eq, region_hash_neq, classifier, none"
        )
    if on == "none":
        return UntilSpec(region=DEFAULT_REGION, on="none")

    region = normalize_region(value.get("region"))
    threshold = value.get("threshold", DEFAULT_UNTIL_THRESHOLD)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise ValueError("until.threshold must be a number")
    threshold_f = float(threshold)
    if threshold_f < 0 or threshold_f > 1:
        raise ValueError("until.threshold must be between 0 and 1")

    stable_frames = value.get("stable_frames", DEFAULT_STABLE_FRAMES)
    if isinstance(stable_frames, bool) or not isinstance(stable_frames, int):
        raise ValueError("until.stable_frames must be a positive integer")
    if stable_frames < 1 or stable_frames > MAX_FRAMES_PER_CALL:
        raise ValueError("until.stable_frames must be a positive integer")

    digest = value.get("hash")
    if digest is not None:
        if not isinstance(digest, str) or not digest.strip():
            raise ValueError("until.hash must be a non-empty string")
        digest = digest.strip().lower()

    classifier = value.get("classifier")
    if classifier is not None:
        if not isinstance(classifier, str):
            raise ValueError(
                "until.classifier must be textbox_likely, battle_likely, or start_menu_likely"
            )
        classifier = classifier.strip().lower()
        if classifier not in CLASSIFIERS:
            raise ValueError(
                "until.classifier must be textbox_likely, battle_likely, or start_menu_likely"
            )

    polarity_raw = value.get("classifier_polarity", "appears")
    if not isinstance(polarity_raw, str):
        raise ValueError("until.classifier_polarity must be 'appears' or 'disappears'")
    polarity = polarity_raw.strip().lower()
    if polarity not in CLASSIFIER_POLARITIES:
        raise ValueError("until.classifier_polarity must be 'appears' or 'disappears'")

    if on in {"region_hash_eq", "region_hash_neq"} and not digest:
        raise ValueError(f"until.hash is required when until.on is {on!r}")
    if on == "classifier" and not classifier:
        raise ValueError("until.classifier is required when until.on is 'classifier'")

    return UntilSpec(
        region=region,
        on=on,
        threshold=threshold_f,
        stable_frames=stable_frames,
        hash=digest,
        classifier=classifier,
        classifier_polarity=polarity,
    )


def parse_hash_regions(value: Any) -> dict[str, tuple[int, int, int, int]]:
    regions = dict(DEFAULT_HASH_REGIONS)
    if value is None:
        return regions
    if not isinstance(value, dict):
        raise ValueError("hash_regions must be an object of name -> [x, y, w, h]")
    for name, box in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("hash_regions keys must be non-empty strings")
        key = name.strip()
        if key in {"full", "bottom", "center"}:
            # Allow overriding the builtins; still keep the names.
            regions[key] = normalize_region(box)
        else:
            regions[key] = normalize_region(box)
    return regions


def _normalize_step(index: int, step: Any, default_gap: int) -> InputStep:
    if not isinstance(step, dict):
        raise ValueError(f"step {index}: each step must be an object with a buttons list")
    wait_flag = bool(step.get("wait"))
    raw_buttons = step.get("buttons")
    allow_empty = wait_flag or raw_buttons == [] or raw_buttons is None and wait_flag
    try:
        if raw_buttons is None and wait_flag:
            buttons = []
        else:
            buttons = normalize_buttons(raw_buttons, allow_empty=True)
        if not buttons and not wait_flag and raw_buttons != []:
            # Missing buttons without wait.
            if raw_buttons is None:
                raise ValueError("at least one button is required")
        hold_frames = normalize_hold_frames(step.get("hold_frames"), default=1)
        gap_frames = normalize_gap_frames(step.get("gap_frames"), default=default_gap)
    except ValueError as exc:
        raise ValueError(f"step {index}: {exc}") from exc
    wait = wait_flag or not buttons
    if not wait and not buttons:
        raise ValueError(f"step {index}: at least one button is required")
    return InputStep(
        buttons=tuple(buttons),
        hold_frames=hold_frames,
        gap_frames=gap_frames,
        wait=wait,
    )


def parse_steps(steps: Any, *, default_gap: int) -> list[InputStep]:
    if not isinstance(steps, list):
        raise ValueError("steps must be a list of chord objects")
    if not steps:
        raise ValueError("steps must not be empty")
    if len(steps) > MAX_INPUT_STEPS:
        raise ValueError(f"steps cannot exceed {MAX_INPUT_STEPS}")
    return [_normalize_step(index, step, default_gap) for index, step in enumerate(steps)]


def _planned_step_frames(steps: list[InputStep]) -> int:
    total = 0
    for step in steps:
        total += step.hold_frames + step.gap_frames
    return total


def call_timeout_for_speed(speed: int, planned_frames: int) -> float:
    """Wall-clock budget. Uncapped/fast calls stay near 20s; 1x can use more."""
    if speed <= 0:
        return DEFAULT_CALL_TIMEOUT_SECONDS
    seconds = planned_frames / (60.0 * speed) + 5.0
    return min(MAX_CALL_TIMEOUT_SECONDS, max(DEFAULT_CALL_TIMEOUT_SECONDS, seconds))


def parse_play_input(payload: dict[str, Any], *, session_speed: int | None = None) -> PlayInput:
    """Validate a send_pyboy_input argument dict. Raises ValueError."""
    screenshot_mode = parse_screenshot_mode(payload.get("screenshot_mode"))
    screenshot_scale = parse_screenshot_scale(payload.get("screenshot_scale"))
    default_speed = (
        DEFAULT_EMULATION_SPEED if session_speed is None else parse_emulation_speed(session_speed)
    )
    emulation_speed = parse_emulation_speed(
        payload.get("emulation_speed"), default=default_speed
    )
    gap_frames = normalize_gap_frames(payload.get("gap_frames"))
    until = parse_until(payload.get("until"))
    interval = payload.get("until_eval_interval", DEFAULT_UNTIL_EVAL_INTERVAL)
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise ValueError(
            f"until_eval_interval must be an integer from {MIN_UNTIL_EVAL_INTERVAL} "
            f"to {MAX_UNTIL_EVAL_INTERVAL}"
        )
    if interval < MIN_UNTIL_EVAL_INTERVAL or interval > MAX_UNTIL_EVAL_INTERVAL:
        raise ValueError(
            f"until_eval_interval must be an integer from {MIN_UNTIL_EVAL_INTERVAL} "
            f"to {MAX_UNTIL_EVAL_INTERVAL}"
        )

    disable_default = bool(payload.get("disable_default_hold_abort"))
    if until is not None and until.on == "none":
        disable_default = True
        until = None

    ocr = bool(payload.get("ocr"))
    hash_regions = parse_hash_regions(payload.get("hash_regions"))

    raw_macro = payload.get("macro")
    macro: str | None
    if raw_macro is None:
        macro = None
    else:
        if not isinstance(raw_macro, str):
            raise ValueError("macro must be 'hold', 'mash', 'steps', or 'buttons'")
        macro = raw_macro.strip().lower()
        if macro not in MACROS:
            raise ValueError("macro must be 'hold', 'mash', 'steps', or 'buttons'")

    steps_arg = payload.get("steps")
    buttons_arg = payload.get("buttons")
    has_steps = isinstance(steps_arg, list) and len(steps_arg) > 0
    # Explicit empty list is still "steps provided".
    steps_provided = steps_arg is not None
    has_buttons = isinstance(buttons_arg, list) and len(buttons_arg) > 0
    wait_flag = bool(payload.get("wait"))

    if steps_provided and has_buttons:
        raise ValueError("provide either top-level buttons or steps, not both")
    if has_steps and macro in {"hold", "mash"}:
        raise ValueError("provide either top-level buttons or steps, not both")

    mash_button = payload.get("mash_button", DEFAULT_MASH_BUTTON)
    mash_button = _normalize_button(mash_button) if mash_button is not None else DEFAULT_MASH_BUTTON
    mash_press = payload.get("mash_press_frames", DEFAULT_MASH_PRESS_FRAMES)
    mash_release = payload.get("mash_release_frames", DEFAULT_MASH_RELEASE_FRAMES)
    for label, value in (
        ("mash_press_frames", mash_press),
        ("mash_release_frames", mash_release),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_HOLD_FRAMES:
            raise ValueError(f"{label} must be an integer from 1 to {MAX_HOLD_FRAMES}")

    hold_frames = normalize_hold_frames(payload.get("hold_frames"), default=1)

    if steps_provided:
        steps = parse_steps(steps_arg, default_gap=gap_frames)
        planned = _planned_step_frames(steps)
        if planned > MAX_FRAMES_PER_CALL:
            raise ValueError(
                f"total frames simulated this call cannot exceed {MAX_FRAMES_PER_CALL}"
            )
        max_frames = normalize_max_frames(payload.get("max_frames"), default=planned)
        if max_frames < planned:
            # Caller asked for a shorter cap than the script; reject rather than truncate.
            raise ValueError(
                f"max_frames ({max_frames}) is smaller than the script's {planned} frames"
            )
        resolved_macro = "steps"
        buttons: list[str] = []
    elif macro == "mash":
        max_frames = normalize_max_frames(
            payload.get("max_frames"), default=MAX_FRAMES_PER_CALL
        )
        planned = max_frames
        steps = []
        buttons = []
        resolved_macro = "mash"
        hold_frames = max_frames
    elif macro == "hold":
        default_max = hold_frames if payload.get("hold_frames") is not None else MAX_FRAMES_PER_CALL
        if payload.get("max_frames") is not None:
            max_frames = normalize_max_frames(payload.get("max_frames"), default=default_max)
        else:
            max_frames = default_max
        if max_frames > MAX_FRAMES_PER_CALL:
            raise ValueError(f"max_frames must be an integer from 1 to {MAX_FRAMES_PER_CALL}")
        buttons = normalize_buttons(buttons_arg, allow_empty=True)
        planned = max_frames
        steps = []
        resolved_macro = "hold"
        hold_frames = max_frames
    elif has_buttons or macro == "buttons":
        buttons = normalize_buttons(buttons_arg, allow_empty=False)
        steps = [
            InputStep(
                buttons=tuple(buttons),
                hold_frames=hold_frames,
                gap_frames=gap_frames,
                wait=False,
            )
        ]
        planned = hold_frames + gap_frames
        if planned > MAX_FRAMES_PER_CALL:
            raise ValueError(
                f"total frames simulated this call cannot exceed {MAX_FRAMES_PER_CALL}"
            )
        max_frames = normalize_max_frames(payload.get("max_frames"), default=planned)
        resolved_macro = "buttons"
    elif wait_flag:
        # Top-level wait only when wait=true (empty buttons=[] without wait is still an error).
        steps = [
            InputStep(
                buttons=(),
                hold_frames=hold_frames,
                gap_frames=gap_frames,
                wait=True,
            )
        ]
        planned = hold_frames + gap_frames
        if planned > MAX_FRAMES_PER_CALL:
            raise ValueError(
                f"total frames simulated this call cannot exceed {MAX_FRAMES_PER_CALL}"
            )
        max_frames = normalize_max_frames(payload.get("max_frames"), default=planned)
        buttons = []
        resolved_macro = "steps"
    else:
        raise ValueError("at least one button is required")

    apply_default = resolved_macro == "hold" and not disable_default
    timeout = call_timeout_for_speed(emulation_speed, min(planned, max_frames))
    timeout_override = payload.get("call_timeout_seconds")
    if timeout_override is not None:
        if not isinstance(timeout_override, (int, float)) or isinstance(timeout_override, bool):
            raise ValueError("call_timeout_seconds must be a positive number")
        timeout = float(timeout_override)
        if timeout <= 0 or timeout > MAX_CALL_TIMEOUT_SECONDS:
            raise ValueError(
                f"call_timeout_seconds must be between 0 exclusive and {MAX_CALL_TIMEOUT_SECONDS}"
            )

    return PlayInput(
        macro=resolved_macro,
        steps=tuple(steps),
        buttons=tuple(buttons),
        hold_frames=hold_frames,
        mash_button=mash_button,
        mash_press_frames=mash_press,
        mash_release_frames=mash_release,
        max_frames=max_frames,
        gap_frames=gap_frames,
        emulation_speed=emulation_speed,
        screenshot_mode=screenshot_mode,
        screenshot_scale=screenshot_scale,
        until=until,
        until_eval_interval=interval,
        disable_default_hold_abort=disable_default,
        apply_default_hold_abort=apply_default,
        default_hold_abort_threshold=DEFAULT_HOLD_ABORT_THRESHOLD,
        default_hold_abort_luma_jump=DEFAULT_HOLD_ABORT_LUMA_JUMP,
        hash_regions=hash_regions,
        ocr=ocr,
        call_timeout_seconds=timeout,
        planned_frames=min(planned, max_frames),
    )


# Re-export boxes so vision can import a single module for named regions.
HASH_FULL = DEFAULT_HASH_REGIONS["full"]
HASH_BOTTOM = BOTTOM_REGION
HASH_CENTER = CENTER_REGION
