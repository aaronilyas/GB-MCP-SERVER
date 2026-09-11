"""Public play intents composed from existing engine primitives.

Runs inside the play instance (needs numpy / LCD). Not a new MCP tool.
"""

from __future__ import annotations

from typing import Any

from gb_mcp.emulator.play_limits import (
    DEFAULT_EMULATION_SPEED,
    DEFAULT_SCREENSHOT_SCALE,
    MAX_FRAMES_PER_CALL,
    MAX_SCREENSHOT_ALL,
    PUBLIC_MASH_PRESS_FRAMES,
    PUBLIC_MASH_RELEASE_FRAMES,
)
from gb_mcp.emulator.vision import (
    capture_native,
    classify,
    command_pane_bottom_right_path,
    command_pane_visible,
)

_HOLD = 12
_DPAD = ("up", "down", "left", "right")
_ADVANCE_WAIT_FRAMES = 90
_ADVANCE_GONE_EVALS = 3
_ADVANCE_EVAL_FRAMES = 8
_SKIP_INTRO_CAP = 10800
_BATTLE_TURN_ITERS = 16
_BATTLE_TURN_FRAME_CAP = 3600


def execute_intent(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None = None,
    monotonic: Any = None,
) -> dict[str, Any]:
    name = getattr(play, "intent", None)
    if name == "advance_text":
        return _advance_text(pyboy, play, session_speed=session_speed, monotonic=monotonic)
    if name == "run_away":
        return _run_away(pyboy, play, session_speed=session_speed, monotonic=monotonic)
    if name == "battle_turn":
        return _battle_turn(pyboy, play, session_speed=session_speed, monotonic=monotonic)
    if name == "battle_until_overworld":
        return _battle_until_overworld(
            pyboy, play, session_speed=session_speed, monotonic=monotonic
        )
    if name == "skip_intro":
        return _skip_intro(pyboy, play, session_speed=session_speed, monotonic=monotonic)
    if name == "enter_door":
        return _enter_door(pyboy, play, session_speed=session_speed, monotonic=monotonic)
    return _brief_wait(
        pyboy, play, session_speed=session_speed, monotonic=monotonic, frames=8
    )


def _run(
    pyboy: Any,
    payload: dict[str, Any],
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    from gb_mcp.emulator.play_runtime import execute_play_command

    kwargs: dict[str, Any] = {"pack_media": False}
    if session_speed is not None:
        kwargs["session_speed"] = session_speed
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return execute_play_command(pyboy, payload, **kwargs)


def _intent_media(play: Any) -> str:
    extra = getattr(play, "extra", None) or {}
    return str(extra.get("media") or "image").strip().lower()


def _intent_wants_gif(play: Any) -> bool:
    return _intent_media(play) == "video"


def _intent_screenshot_mode(play: Any, default: str = "final") -> str:
    if _intent_wants_gif(play):
        return "keyframes"
    mode = str(getattr(play, "screenshot_mode", "") or "")
    if mode in {"keyframes", "all"}:
        return mode
    return default


def _base_payload(play: Any, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "emulation_speed": getattr(play, "emulation_speed", DEFAULT_EMULATION_SPEED),
        "screenshot_scale": getattr(play, "screenshot_scale", DEFAULT_SCREENSHOT_SCALE),
        "screenshot_mode": extra.pop("screenshot_mode", _intent_screenshot_mode(play)),
        "media": extra.pop("media", _intent_media(play)),
    }
    payload.update(extra)
    return payload


def _wait_payload(play: Any, frames: int, **until: Any) -> dict[str, Any]:
    body = _base_payload(
        play,
        wait=True,
        hold_frames=frames,
        max_frames=frames,
        screenshot_mode="interrupt_and_final",
    )
    if until:
        body["until"] = until
    return body


def _merge(
    parts: list[dict[str, Any]],
    *,
    want_gif: bool = False,
    screenshot_mode: str = "final",
) -> dict[str, Any]:
    from gb_mcp.emulator.play_runtime import pack_action_media, strip_forbidden_keys

    if not parts:
        return {"frames_advanced": 0, "stop_reason": "completed", "until_fired": False}
    frames = 0
    natives: list[bytes] = []
    pngs: list[bytes] = []
    for part in parts:
        frames += int(part.get("frames_advanced") or 0)
        for blob in part.get("pngs_native") or []:
            if isinstance(blob, (bytes, bytearray)) and blob:
                natives.append(bytes(blob))
        for blob in part.get("pngs") or []:
            if isinstance(blob, (bytes, bytearray)) and blob:
                pngs.append(bytes(blob))
    last = dict(parts[-1])
    last["frames_advanced"] = frames
    last.pop("gif", None)
    last.pop("gifs", None)
    keep_strip = screenshot_mode in {"keyframes", "all"}
    if pngs:
        packed = pack_action_media(pngs, want_gif=bool(want_gif) and len(pngs) >= 2)
        last["pngs"] = packed.get("pngs") or pngs[-1:]
        last["screenshot_count"] = len(last["pngs"])
        gif = packed.get("gif") if want_gif else None
        if gif:
            last["gif"] = gif
            if natives:
                last["pngs_native"] = natives[-1:]
            last["screenshot_mode"] = "final"
        elif natives:
            last["pngs_native"] = (
                natives[-MAX_SCREENSHOT_ALL:] if keep_strip else natives[-1:]
            )
            last["screenshot_mode"] = screenshot_mode if keep_strip else "final"
        else:
            last["screenshot_mode"] = screenshot_mode if keep_strip else "final"
        return strip_forbidden_keys(last)
    if natives:
        last["pngs_native"] = natives[-MAX_SCREENSHOT_ALL:] if keep_strip else natives[-1:]
    last["screenshot_mode"] = screenshot_mode if keep_strip else "final"
    return strip_forbidden_keys(last)


def _merged(play: Any, parts: list[dict[str, Any]]) -> dict[str, Any]:
    return _merge(
        parts,
        want_gif=_intent_wants_gif(play),
        screenshot_mode=_intent_screenshot_mode(play),
    )


def _button_steps(buttons: list[str], hold: int = _HOLD) -> list[dict[str, Any]]:
    return [{"buttons": [name], "hold_frames": hold, "gap_frames": 4} for name in buttons]


def _brief_wait(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
    frames: int = 6,
) -> dict[str, Any]:
    return _run(
        pyboy,
        _wait_payload(play, frames),
        session_speed=session_speed,
        monotonic=monotonic,
    )


def _mash_a(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
    max_frames: int,
    until: dict[str, Any],
) -> dict[str, Any]:
    return _run(
        pyboy,
        _base_payload(
            play,
            macro="mash",
            mash_button="a",
            mash_press_frames=PUBLIC_MASH_PRESS_FRAMES,
            mash_release_frames=PUBLIC_MASH_RELEASE_FRAMES,
            max_frames=max(16, min(max_frames, MAX_FRAMES_PER_CALL)),
            until=until,
        ),
        session_speed=session_speed,
        monotonic=monotonic,
    )


def _advance_text(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    flags = classify(capture_native(pyboy))
    if not flags.get("textbox_likely"):
        wait = _run(
            pyboy,
            _wait_payload(
                play,
                _ADVANCE_WAIT_FRAMES,
                on="classifier",
                classifier="textbox_likely",
                classifier_polarity="appears",
            ),
            session_speed=session_speed,
            monotonic=monotonic,
        )
        if not classify(capture_native(pyboy)).get("textbox_likely"):
            return wait
        parts: list[dict[str, Any]] = [wait]
    else:
        parts = []

    budget = min(
        MAX_FRAMES_PER_CALL,
        int(getattr(play, "max_frames", MAX_FRAMES_PER_CALL) or MAX_FRAMES_PER_CALL),
    )
    used = sum(int(part.get("frames_advanced") or 0) for part in parts)
    gone_evals = 0
    while used < budget:
        flags = classify(capture_native(pyboy))
        remaining = budget - used
        if remaining <= 0:
            break
        if flags.get("textbox_likely"):
            gone_evals = 0
            mash = _mash_a(
                pyboy,
                play,
                session_speed=session_speed,
                monotonic=monotonic,
                max_frames=remaining,
                until={
                    "on": "classifier",
                    "classifier": "textbox_likely",
                    "classifier_polarity": "disappears",
                },
            )
            parts.append(mash)
            advanced = int(mash.get("frames_advanced") or 0)
            used += advanced
            if advanced <= 0:
                break
            continue
        wait = _run(
            pyboy,
            _wait_payload(play, min(_ADVANCE_EVAL_FRAMES, remaining)),
            session_speed=session_speed,
            monotonic=monotonic,
        )
        parts.append(wait)
        advanced = int(wait.get("frames_advanced") or 0)
        used += max(advanced, 1)
        if classify(capture_native(pyboy)).get("textbox_likely"):
            gone_evals = 0
            continue
        gone_evals += 1
        if gone_evals >= _ADVANCE_GONE_EVALS:
            break
    if not parts:
        return _brief_wait(
            pyboy, play, session_speed=session_speed, monotonic=monotonic, frames=8
        )
    return _merged(play, parts)


def _dismissable_overlay(flags: dict[str, Any]) -> bool:
    return bool(
        flags.get("battle_likely")
        or flags.get("start_menu_likely")
        or flags.get("textbox_likely")
    )


def _run_away(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    opening = classify(capture_native(pyboy))
    if not _dismissable_overlay(opening):
        return _brief_wait(
            pyboy, play, session_speed=session_speed, monotonic=monotonic, frames=6
        )

    parts: list[dict[str, Any]] = []
    for _attempt in range(2):
        frame = capture_native(pyboy)
        flags = classify(frame)
        # Textbox often masks the combat HUD classifier; clear it before flee inputs.
        if flags.get("textbox_likely"):
            parts.append(
                _mash_a(
                    pyboy,
                    play,
                    session_speed=session_speed,
                    monotonic=monotonic,
                    max_frames=min(
                        240,
                        int(
                            getattr(play, "max_frames", MAX_FRAMES_PER_CALL)
                            or MAX_FRAMES_PER_CALL
                        ),
                    ),
                    until={
                        "on": "classifier",
                        "classifier": "textbox_likely",
                        "classifier_polarity": "disappears",
                    },
                )
            )
            frame = capture_native(pyboy)
            flags = classify(frame)
            if (
                not flags.get("battle_likely")
                and not flags.get("start_menu_likely")
                and not command_pane_visible(frame)
            ):
                break

        parts.append(
            _run(
                pyboy,
                _base_payload(
                    play,
                    steps=_button_steps(["b"], hold=12),
                ),
                session_speed=session_speed,
                monotonic=monotonic,
            )
        )
        parts.append(
            _run(
                pyboy,
                _wait_payload(
                    play,
                    32,
                    on="classifier",
                    classifier="battle_likely",
                    classifier_polarity="disappears",
                ),
                session_speed=session_speed,
                monotonic=monotonic,
            )
        )

        frame = capture_native(pyboy)
        if command_pane_visible(frame):
            path = command_pane_bottom_right_path(frame)
            if path:
                parts.append(
                    _run(
                        pyboy,
                        _base_payload(
                            play,
                            steps=_button_steps(path),
                        ),
                        session_speed=session_speed,
                        monotonic=monotonic,
                    )
                )

        parts.append(
            _run(
                pyboy,
                _base_payload(
                    play,
                    wait=True,
                    hold_frames=96,
                    max_frames=96,
                    until={"on": "overworld", "stable_frames": 4},
                    screenshot_mode="interrupt_and_final",
                ),
                session_speed=session_speed,
                monotonic=monotonic,
            )
        )
        if not classify(capture_native(pyboy)).get("battle_likely"):
            break

    if not parts:
        return _brief_wait(
            pyboy, play, session_speed=session_speed, monotonic=monotonic, frames=6
        )
    return _merged(play, parts)


def _turn_event_ready(frame: Any) -> bool:
    flags = classify(frame)
    if command_pane_visible(frame):
        return True
    if flags.get("textbox_likely"):
        return True
    if not flags.get("battle_likely"):
        return True
    if flags.get("window_occluded_likely"):
        return True
    return False


def _wait_turn_event(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
    budget: int = 180,
) -> dict[str, Any]:
    """Wait until command pane, HUD gone, textbox, or fade — not until=stable."""
    parts: list[dict[str, Any]] = []
    used = 0
    while used < budget:
        if _turn_event_ready(capture_native(pyboy)):
            break
        slice_n = min(8, budget - used)
        part = _run(
            pyboy,
            _wait_payload(play, slice_n),
            session_speed=session_speed,
            monotonic=monotonic,
        )
        parts.append(part)
        advanced = int(part.get("frames_advanced") or 0)
        used += advanced
        if advanced <= 0:
            break
        if _turn_event_ready(capture_native(pyboy)):
            break
    if not parts:
        return {
            "frames_advanced": 0,
            "stop_reason": "completed",
            "until_fired": False,
            "pngs": [],
            "pngs_native": [],
        }
    return _merged(play, parts)


def _battle_turn(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    frame = capture_native(pyboy)
    flags = classify(frame)
    if not flags.get("battle_likely"):
        return _brief_wait(
            pyboy, play, session_speed=session_speed, monotonic=monotonic, frames=6
        )

    parts: list[dict[str, Any]] = []
    total = 0
    for _ in range(_BATTLE_TURN_ITERS):
        if total >= _BATTLE_TURN_FRAME_CAP:
            break
        frame = capture_native(pyboy)
        flags = classify(frame)
        if not flags.get("battle_likely") and not flags.get("textbox_likely"):
            break

        if flags.get("textbox_likely"):
            mash = _mash_a(
                pyboy,
                play,
                session_speed=session_speed,
                monotonic=monotonic,
                max_frames=min(240, _BATTLE_TURN_FRAME_CAP - total),
                until={
                    "on": "classifier",
                    "classifier": "textbox_likely",
                    "classifier_polarity": "disappears",
                },
            )
            parts.append(mash)
            total += int(mash.get("frames_advanced") or 0)
        else:
            # Command pane or bare HUD: confirm with A.
            act = _run(
                pyboy,
                _base_payload(
                    play,
                    steps=_button_steps(["a"], hold=12),
                ),
                session_speed=session_speed,
                monotonic=monotonic,
            )
            parts.append(act)
            total += int(act.get("frames_advanced") or 0)

        settle = _wait_turn_event(
            pyboy,
            play,
            session_speed=session_speed,
            monotonic=monotonic,
            budget=min(180, _BATTLE_TURN_FRAME_CAP - total),
        )
        parts.append(settle)
        total += int(settle.get("frames_advanced") or 0)

        end = classify(capture_native(pyboy))
        if not end.get("battle_likely") and not end.get("textbox_likely"):
            break

    if not parts:
        return _brief_wait(
            pyboy, play, session_speed=session_speed, monotonic=monotonic, frames=6
        )
    return _merged(play, parts)


def _stamp_reason(result: dict[str, Any], reason: str) -> dict[str, Any]:
    result["stop_reason"] = reason
    result["stop_detail"] = reason
    return result


def _battle_until_overworld(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    flags = classify(capture_native(pyboy))
    if not flags.get("battle_likely") and not flags.get("textbox_likely"):
        wait = _brief_wait(
            pyboy, play, session_speed=session_speed, monotonic=monotonic, frames=6
        )
        return _stamp_reason(wait, "overworld")

    budget = min(
        MAX_FRAMES_PER_CALL,
        int(getattr(play, "max_frames", MAX_FRAMES_PER_CALL) or MAX_FRAMES_PER_CALL),
    )
    result = _mash_a(
        pyboy,
        play,
        session_speed=session_speed,
        monotonic=monotonic,
        max_frames=budget,
        until={"on": "overworld", "stable_frames": 4},
    )
    end = classify(capture_native(pyboy))
    if not end.get("battle_likely") and not end.get("textbox_likely"):
        reason = "overworld"
    elif result.get("stop_detail") == "blocked" or result.get("stop_reason") == "blocked":
        reason = "blocked"
    else:
        reason = "cap"
    return _stamp_reason(result, reason)


def _skip_intro(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    used = 0
    cap = min(
        _SKIP_INTRO_CAP,
        int(getattr(play, "max_frames", MAX_FRAMES_PER_CALL) or MAX_FRAMES_PER_CALL),
    )
    while used < cap:
        flags = classify(capture_native(pyboy))
        if flags.get("start_menu_likely") or flags.get("textbox_likely"):
            break
        pulse = _run(
            pyboy,
            _base_payload(
                play,
                steps=[
                    {"buttons": ["start"], "hold_frames": 4, "gap_frames": 4},
                    {"buttons": ["a"], "hold_frames": 4, "gap_frames": 4},
                ],
            ),
            session_speed=session_speed,
            monotonic=monotonic,
        )
        parts.append(pulse)
        advanced = int(pulse.get("frames_advanced") or 0)
        used += advanced
        if advanced <= 0:
            break
    if not parts:
        return _brief_wait(
            pyboy, play, session_speed=session_speed, monotonic=monotonic, frames=6
        )
    return _merged(play, parts)


def _door_direction(play: Any) -> str:
    buttons = tuple(getattr(play, "buttons", ()) or ())
    for name in buttons:
        if name in _DPAD:
            return name
    return "up"


def _enter_door(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    # Agent should already face the entrance; this does not imply a 16-frame step onto the mat.
    direction = _door_direction(play)
    budget = int(getattr(play, "max_frames", MAX_FRAMES_PER_CALL) or MAX_FRAMES_PER_CALL)
    hold_budget = max(32, min(budget, 360))
    fade_wait = max(32, min(budget, MAX_FRAMES_PER_CALL))
    hold = _run(
        pyboy,
        _base_payload(
            play,
            macro="hold",
            buttons=[direction],
            hold_frames=hold_budget,
            max_frames=hold_budget,
            until={"on": "luma_jump"},
        ),
        session_speed=session_speed,
        monotonic=monotonic,
    )
    settle = _run(
        pyboy,
        _base_payload(
            play,
            wait=True,
            hold_frames=fade_wait,
            max_frames=fade_wait,
            until={"on": "overworld", "stable_frames": 4},
            screenshot_mode="interrupt_and_final",
        ),
        session_speed=session_speed,
        monotonic=monotonic,
    )
    return _merged(play, [hold, settle])
