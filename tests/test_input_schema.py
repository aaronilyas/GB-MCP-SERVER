"""Validation for the frozen send_pyboy_input request schema."""

from __future__ import annotations

import pytest

from gb_mcp.emulator.input_schema import parse_play_input
from gb_mcp.emulator.play_limits import (
    DEFAULT_EMULATION_SPEED,
    DEFAULT_HOLD_ABORT_THRESHOLD,
    DEFAULT_SCREENSHOT_SCALE,
    MAX_HOLD_FRAMES,
    MAX_INPUT_STEPS,
)


def test_rejects_more_than_max_steps() -> None:
    with pytest.raises(ValueError, match="steps cannot exceed"):
        parse_play_input({"steps": [{"buttons": ["a"]}] * (MAX_INPUT_STEPS + 1)})


def test_rejects_hold_frames_above_cap() -> None:
    with pytest.raises(ValueError, match="hold_frames"):
        parse_play_input({"buttons": ["up"], "hold_frames": MAX_HOLD_FRAMES + 1})


def test_rejects_buttons_and_steps_together() -> None:
    with pytest.raises(ValueError, match="not both"):
        parse_play_input(
            {"buttons": ["a"], "steps": [{"buttons": ["b"]}]}
        )


def test_empty_wait_step_accepted() -> None:
    play = parse_play_input(
        {"steps": [{"buttons": [], "hold_frames": 12}, {"buttons": ["a"], "hold_frames": 4}]}
    )
    assert play.macro == "steps"
    assert play.steps[0].wait is True
    assert play.steps[0].buttons == ()
    assert play.steps[0].hold_frames == 12
    assert play.steps[1].buttons == ("a",)


def test_wait_true_step_accepted() -> None:
    play = parse_play_input({"steps": [{"wait": True, "hold_frames": 8}]})
    assert play.steps[0].wait is True
    assert play.steps[0].hold_frames == 8


def test_top_level_empty_buttons_still_rejected() -> None:
    with pytest.raises(ValueError, match="at least one button"):
        parse_play_input({"buttons": []})


def test_hold_macro_defaults_and_default_abort() -> None:
    play = parse_play_input(
        {"macro": "hold", "buttons": ["up"], "hold_frames": 200}
    )
    assert play.macro == "hold"
    assert play.buttons == ("up",)
    assert play.max_frames == 200
    assert play.apply_default_hold_abort is True
    assert play.default_hold_abort_threshold == DEFAULT_HOLD_ABORT_THRESHOLD
    assert play.emulation_speed == DEFAULT_EMULATION_SPEED
    assert play.screenshot_scale == DEFAULT_SCREENSHOT_SCALE


def test_hold_abort_disabled_via_until_none() -> None:
    play = parse_play_input(
        {"macro": "hold", "buttons": ["up"], "max_frames": 40, "until": {"on": "none"}}
    )
    assert play.until is None
    assert play.apply_default_hold_abort is False
    assert play.disable_default_hold_abort is True


def test_mash_macro() -> None:
    play = parse_play_input({"macro": "mash", "max_frames": 80, "mash_button": "A"})
    assert play.macro == "mash"
    assert play.mash_button == "a"
    assert play.max_frames == 80
    assert play.apply_default_hold_abort is False


def test_uncapped_speed_aliases() -> None:
    assert parse_play_input({"buttons": ["a"], "emulation_speed": "uncapped"}).emulation_speed == 0
    assert parse_play_input({"buttons": ["a"], "emulation_speed": 0}).emulation_speed == 0
    with pytest.raises(ValueError, match="emulation_speed"):
        parse_play_input({"buttons": ["a"], "emulation_speed": 3})


def test_old_buttons_path_still_parses() -> None:
    play = parse_play_input({"buttons": ["A", "Up"], "hold_frames": 3, "screenshot_mode": "final"})
    assert play.macro == "buttons"
    assert play.steps[0].buttons == ("a", "up")
    assert play.steps[0].hold_frames == 3
    assert play.screenshot_mode == "final"
