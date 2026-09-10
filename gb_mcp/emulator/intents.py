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
    fight_cursor_cell,
    fight_menu_visible,
)

_HOLD = 12
_TOWARD_RUN: dict[str | None, list[str]] = {
    "fight": ["down", "right", "a"],
    "pkmn": ["down", "a"],
    "item": ["right", "a"],
    "run": ["a"],
    None: ["down", "right", "a"],
}


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
    return _run(
        pyboy,
        _base_payload(play, wait_frames=8),
        session_speed=session_speed,
        monotonic=monotonic,
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


def _base_payload(play: Any, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "emulation_speed": getattr(play, "emulation_speed", DEFAULT_EMULATION_SPEED),
        "screenshot_scale": getattr(play, "screenshot_scale", DEFAULT_SCREENSHOT_SCALE),
        "screenshot_mode": extra.pop("screenshot_mode", "keyframes"),
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


def _merge(parts: list[dict[str, Any]]) -> dict[str, Any]:
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
    if pngs:
        from gb_mcp.emulator.play_runtime import pack_action_media, strip_forbidden_keys

        packed = pack_action_media(pngs, want_gif=len(pngs) >= 2)
        last["pngs"] = packed.get("pngs") or pngs[-1:]
        last["screenshot_count"] = len(last["pngs"])
        gif = packed.get("gif")
        if gif:
            last["gif"] = gif
            if natives:
                last["pngs_native"] = natives[-1:]
        elif natives:
            last["pngs_native"] = natives[-MAX_SCREENSHOT_ALL:]
        last["screenshot_mode"] = "final" if gif else "keyframes"
        return strip_forbidden_keys(last)
    if natives:
        last["pngs_native"] = natives[-MAX_SCREENSHOT_ALL:]
    last["screenshot_mode"] = "keyframes"
    return last


def _advance_text(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    flags = classify(capture_native(pyboy))
    if not flags.get("textbox_likely"):
        return _run(
            pyboy,
            _wait_payload(play, 8),
            session_speed=session_speed,
            monotonic=monotonic,
        )
    budget = int(getattr(play, "max_frames", MAX_FRAMES_PER_CALL) or MAX_FRAMES_PER_CALL)
    mash = _run(
        pyboy,
        _base_payload(
            play,
            macro="mash",
            mash_button="a",
            mash_press_frames=PUBLIC_MASH_PRESS_FRAMES,
            mash_release_frames=PUBLIC_MASH_RELEASE_FRAMES,
            max_frames=max(16, min(budget, MAX_FRAMES_PER_CALL)),
            until={
                "on": "classifier",
                "classifier": "textbox_likely",
                "classifier_polarity": "disappears",
            },
            screenshot_mode="keyframes",
        ),
        session_speed=session_speed,
        monotonic=monotonic,
    )
    settle = _run(
        pyboy,
        _wait_payload(play, 16, on="stable", stable_frames=4),
        session_speed=session_speed,
        monotonic=monotonic,
    )
    return _merge([mash, settle])


def _button_steps(buttons: list[str], hold: int = _HOLD) -> list[dict[str, Any]]:
    return [{"buttons": [name], "hold_frames": hold, "gap_frames": 4} for name in buttons]


def _still_battle(pyboy: Any) -> bool:
    return bool(classify(capture_native(pyboy)).get("battle_likely"))


def _run_away(
    pyboy: Any,
    play: Any,
    *,
    session_speed: int | None,
    monotonic: Any,
) -> dict[str, Any]:
    flags = classify(capture_native(pyboy))
    if not flags.get("battle_likely"):
        return _run(
            pyboy,
            _wait_payload(play, 4),
            session_speed=session_speed,
            monotonic=monotonic,
        )
    parts: list[dict[str, Any]] = []
    for _attempt in range(2):
        if not _still_battle(pyboy):
            break
        cursor = fight_cursor_cell(capture_native(pyboy))
        nav = _TOWARD_RUN.get(cursor, _TOWARD_RUN[None])
        parts.append(
            _run(
                pyboy,
                _base_payload(
                    play,
                    steps=_button_steps(nav),
                    screenshot_mode="keyframes",
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
                    hold_frames=48,
                    max_frames=48,
                    until={"on": "overworld", "stable_frames": 4},
                    screenshot_mode="interrupt_and_final",
                ),
                session_speed=session_speed,
                monotonic=monotonic,
            )
        )
        if not _still_battle(pyboy):
            break
    if not parts:
        return _run(
            pyboy,
            _wait_payload(play, 4),
            session_speed=session_speed,
            monotonic=monotonic,
        )
    return _merge(parts)


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
        return _run(
            pyboy,
            _wait_payload(play, 4),
            session_speed=session_speed,
            monotonic=monotonic,
        )
    presses = ["a", "a"] if fight_menu_visible(frame) else ["a"]
    act = _run(
        pyboy,
        _base_payload(
            play,
            steps=_button_steps(presses, hold=12),
            screenshot_mode="keyframes",
        ),
        session_speed=session_speed,
        monotonic=monotonic,
    )
    wait = _run(
        pyboy,
        _base_payload(
            play,
            wait=True,
            hold_frames=180,
            max_frames=180,
            until={"on": "stable", "stable_frames": 6},
            screenshot_mode="keyframes",
        ),
        session_speed=session_speed,
        monotonic=monotonic,
    )
    return _merge([act, wait])
