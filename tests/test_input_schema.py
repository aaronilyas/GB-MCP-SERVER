"""Validation for the frozen send_pyboy_input request schema."""

from __future__ import annotations

import pytest

from gb_mcp.emulator.input_schema import (
    PlayArgs,
    parse_play_args,
    parse_play_input,
    play_input_from_args,
)
from gb_mcp.emulator.play_limits import (
    DEFAULT_EMULATION_SPEED,
    DEFAULT_HOLD_ABORT_LUMA_JUMP,
    DEFAULT_HOLD_ABORT_THRESHOLD,
    DEFAULT_MASH_BUTTON,
    DEFAULT_MASH_PRESS_FRAMES,
    DEFAULT_REGION,
    DEFAULT_SCREENSHOT_MODE,
    DEFAULT_SCREENSHOT_SCALE,
    DEFAULT_UNTIL_EVAL_INTERVAL,
    MAX_GAP_FRAMES,
    MAX_HOLD_FRAMES,
    MAX_INPUT_STEPS,
    PUBLIC_MASH_PRESS_FRAMES,
    PUBLIC_MASH_RELEASE_FRAMES,
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
    assert play.default_hold_abort_luma_jump == DEFAULT_HOLD_ABORT_LUMA_JUMP
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


def test_parse_play_args_buttons_defaults() -> None:
    args = parse_play_args({"buttons": ["up"]})
    assert args.buttons == ("up",)
    assert args.frames == 16
    assert args.media == "image"
    assert "mash_press_frames" not in args.__dataclass_fields__
    play = play_input_from_args(args)
    assert play.macro == "buttons"
    assert play.buttons == ("up",)
    assert play.hold_frames == 16
    assert play.steps[0].buttons == ("up",)
    assert play.steps[0].hold_frames == 16
    assert play.screenshot_scale == DEFAULT_SCREENSHOT_SCALE
    assert play.screenshot_scale == 4
    assert play.emulation_speed == DEFAULT_EMULATION_SPEED
    assert play.emulation_speed == 0
    assert play.screenshot_mode == DEFAULT_SCREENSHOT_MODE
    assert play.extra["media"] == "image"


def test_parse_play_args_empty_buttons_is_wait() -> None:
    args = parse_play_args({"buttons": []})
    play = play_input_from_args(args)
    assert play.macro == "steps"
    assert len(play.steps) == 1
    assert play.steps[0].wait is True
    assert play.steps[0].buttons == ()
    assert play.steps[0].hold_frames == 16


def test_parse_play_args_rejects_buttons_and_steps_together() -> None:
    with pytest.raises(ValueError, match="not both"):
        parse_play_args({"buttons": ["a"], "steps": [{"buttons": ["b"]}]})


def test_parse_play_args_mash_true() -> None:
    args = parse_play_args({"mash": True})
    assert isinstance(args, PlayArgs)
    assert args.mash is True
    assert "mash_press_frames" not in PlayArgs.__dataclass_fields__
    assert "mash_release_frames" not in PlayArgs.__dataclass_fields__
    play = play_input_from_args(args)
    assert play.macro == "mash"
    assert play.mash_button == DEFAULT_MASH_BUTTON
    assert play.mash_press_frames == PUBLIC_MASH_PRESS_FRAMES
    assert play.mash_release_frames == PUBLIC_MASH_RELEASE_FRAMES
    assert play.mash_press_frames != DEFAULT_MASH_PRESS_FRAMES
    assert play.screenshot_mode == "keyframes"


@pytest.mark.parametrize(
    ("public", "on", "classifier"),
    [
        ("battle", "classifier", "battle_likely"),
        ("textbox", "classifier", "textbox_likely"),
        ("menu", "classifier", "start_menu_likely"),
        ("stable", "stable", None),
        ("fade", "luma_jump", None),
        ("blocked", "blocked", None),
    ],
)
def test_parse_play_args_until_mapping(
    public: str, on: str, classifier: str | None
) -> None:
    args = parse_play_args({"buttons": ["a"], "until": public})
    assert args.until == public
    assert not hasattr(args, "classifier")
    assert "battle_likely" not in args.__dataclass_fields__
    play = play_input_from_args(args)
    assert play.until is not None
    assert play.until.on == on
    assert play.until.classifier == classifier
    if classifier is not None:
        assert play.until.classifier_polarity == "appears"
    if public == "fade":
        assert play.until.region == DEFAULT_REGION


def test_parse_play_args_media_video_and_default_image() -> None:
    default_args = parse_play_args({"buttons": ["a"]})
    assert default_args.media == "image"
    assert play_input_from_args(default_args).extra["media"] == "image"

    video_args = parse_play_args({"buttons": ["a"], "media": "video"})
    assert video_args.media == "video"
    play = play_input_from_args(video_args)
    assert play.extra["media"] == "video"
    assert play.screenshot_mode == "final"


def test_parse_play_args_ignores_internal_payload_keys() -> None:
    play = play_input_from_args(
        parse_play_args(
            {
                "buttons": ["a"],
                "hash": "deadbeef",
                "hash_regions": {"custom": [0, 0, 8, 8]},
                "ocr": True,
                "screenshot_mode": "all",
                "until_eval_interval": 1,
                "hold_frames": 3,
                "mash_press_frames": 20,
                "disable_default_hold_abort": True,
            }
        )
    )
    assert play.screenshot_mode == "final"
    assert play.ocr is False
    assert play.until_eval_interval == DEFAULT_UNTIL_EVAL_INTERVAL
    assert "custom" not in play.hash_regions
    assert play.hold_frames == 16
    assert play.mash_press_frames == DEFAULT_MASH_PRESS_FRAMES
    assert play.until is None


def test_public_gap_cap_is_180() -> None:
    assert MAX_GAP_FRAMES == 180
    args = parse_play_args({"buttons": ["a"], "gap": 180})
    assert args.gap == 180
    with pytest.raises(ValueError, match="gap"):
        parse_play_args({"buttons": ["a"], "gap": 181})


def test_public_long_direction_is_hold_with_abort() -> None:
    tap = play_input_from_args(parse_play_args({"buttons": ["up"], "frames": 16}))
    assert tap.macro == "buttons"
    assert tap.apply_default_hold_abort is False
    assert tap.screenshot_mode == DEFAULT_SCREENSHOT_MODE

    hold = play_input_from_args(parse_play_args({"buttons": ["up"], "frames": 240}))
    assert hold.macro == "hold"
    assert hold.buttons == ("up",)
    assert hold.max_frames == 240
    assert hold.apply_default_hold_abort is True
    assert hold.screenshot_mode == "keyframes"

    chord = play_input_from_args(parse_play_args({"buttons": ["a", "up"], "frames": 240}))
    assert chord.macro == "buttons"
    assert chord.apply_default_hold_abort is False

    ab = play_input_from_args(parse_play_args({"buttons": ["a"], "frames": 80}))
    assert ab.macro == "hold"
    assert ab.apply_default_hold_abort is True

    short_ab = play_input_from_args(parse_play_args({"buttons": ["a", "b"], "frames": 8}))
    assert short_ab.macro == "buttons"
    assert short_ab.apply_default_hold_abort is False


def test_public_until_polarity_and_aliases() -> None:
    appears = parse_play_args({"buttons": ["a"], "until": "textbox"})
    assert appears.until_polarity == "appears"
    play = play_input_from_args(appears)
    assert play.until is not None
    assert play.until.classifier == "textbox_likely"
    assert play.until.classifier_polarity == "appears"

    gone = parse_play_args(
        {"mash": True, "until": "textbox", "until_polarity": "disappears"}
    )
    play = play_input_from_args(gone)
    assert play.until is not None
    assert play.until.classifier_polarity == "disappears"
    assert play.mash_press_frames == PUBLIC_MASH_PRESS_FRAMES

    alias = parse_play_args({"buttons": ["a"], "until": "textbox_end"})
    assert alias.until == "textbox"
    assert alias.until_polarity == "disappears"
    play = play_input_from_args(alias)
    assert play.until is not None
    assert play.until.classifier_polarity == "disappears"

    overworld = parse_play_args({"buttons": ["a"], "until": "overworld"})
    play = play_input_from_args(overworld)
    assert play.until is not None
    assert play.until.on == "overworld"


def test_public_intent_parse() -> None:
    args = parse_play_args({"intent": "advance_text"})
    assert args.intent == "advance_text"
    play = play_input_from_args(args)
    assert play.intent == "advance_text"
    run = parse_play_args({"intent": "run_away"})
    assert run.intent == "run_away"
    turn = parse_play_args({"intent": "battle_turn", "buttons": ["a"]})
    assert turn.intent == "battle_turn"
    with pytest.raises(ValueError, match="intent"):
        parse_play_args({"intent": "pathfind"})


def test_public_video_keeps_final_for_gif_path() -> None:
    play = play_input_from_args(
        parse_play_args({"buttons": ["up"], "frames": 240, "media": "video"})
    )
    assert play.macro == "hold"
    assert play.extra["media"] == "video"
    assert play.screenshot_mode == "final"
