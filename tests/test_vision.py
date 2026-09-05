"""Framebuffer classifiers, until-eval, and screenshot packaging."""

from __future__ import annotations

import hashlib
import io

import numpy as np
from PIL import Image as PILImage

from gb_mcp.emulator.input_schema import parse_play_input
from gb_mcp.emulator.play_limits import (
    BOTTOM_REGION,
    CENTER_REGION,
    DEFAULT_REGION,
    MAX_SCREENSHOT_ALL,
    NATIVE_HEIGHT,
    NATIVE_WIDTH,
)
from gb_mcp.emulator.vision import (
    ScreenshotPlan,
    UntilMonitor,
    capture_native,
    classify,
    encode_png,
    hash_named_regions,
    pixel_delta_fraction,
    region_hash,
    scale_nearest,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _solid(color: tuple[int, int, int]) -> np.ndarray:
    frame = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 3), dtype=np.uint8)
    frame[:, :] = color
    return frame


def _dialogue_bar(base: tuple[int, int, int] = (80, 160, 80)) -> np.ndarray:
    frame = _solid(base)
    frame[96:144, :, :] = (16, 16, 16)
    frame[100:140, 8:152, :] = (248, 248, 248)
    return frame


def _battle() -> np.ndarray:
    frame = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 3), dtype=np.uint8)
    frame[:72, :, :] = (40, 40, 80)
    frame[72:, :, :] = (80, 140, 80)
    frame[18:22, 40:140, :] = (248, 248, 248)
    frame[100:104, 20:120, :] = (248, 248, 248)
    return frame


def _start_menu(base: tuple[int, int, int] = (80, 160, 80)) -> np.ndarray:
    frame = _solid(base)
    frame[:, :80, :] = (248, 248, 248)
    return frame


def test_textbox_and_pixel_delta_on_dialogue_bar() -> None:
    green = _solid((32, 200, 32))
    dialogue = _dialogue_bar((32, 200, 32))
    assert classify(green)["textbox_likely"] is False
    assert classify(dialogue)["textbox_likely"] is True
    assert pixel_delta_fraction(green, dialogue, DEFAULT_REGION) > 0.08


def test_until_monitor_default_hold_abort() -> None:
    play = parse_play_input(
        {"macro": "hold", "buttons": ["up"], "max_frames": 200}
    )
    assert play.apply_default_hold_abort is True
    monitor = UntilMonitor(play, _solid((32, 200, 32)))
    decision = monitor.evaluate(_solid((200, 16, 16)), 0)
    assert decision is not None
    assert decision.reason == "default_hold_abort"
    assert decision.until_fired is True


def test_until_monitor_caller_until_wins_over_default_abort() -> None:
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 200,
            "until": {"on": "pixel_delta_above", "threshold": 0.08},
        }
    )
    assert play.apply_default_hold_abort is True
    monitor = UntilMonitor(play, _solid((32, 200, 32)))
    decision = monitor.evaluate(_solid((200, 16, 16)), 0)
    assert decision is not None
    assert decision.reason == "screen_change"
    assert decision.until_fired is True


def test_region_hashes_identical_and_dialogue_bottom() -> None:
    overworld = _solid((70, 170, 70))
    regions = {"full": DEFAULT_REGION, "bottom": BOTTOM_REGION, "center": CENTER_REGION}
    first = hash_named_regions(overworld, regions)
    second = hash_named_regions(overworld.copy(), regions)
    assert first["full"] == second["full"]
    assert first["full"] == region_hash(overworld, DEFAULT_REGION)

    pixel = _solid((0, 0, 0))
    pixel[5, 7] = (1, 2, 3)
    assert region_hash(pixel, (7, 5, 1, 1)) == hashlib.blake2s(
        bytes((1, 2, 3)), digest_size=8
    ).hexdigest()

    dialogue = _dialogue_bar((70, 170, 70))
    assert region_hash(overworld, BOTTOM_REGION) != region_hash(dialogue, BOTTOM_REGION)
    # CENTER_REGION is (40, 32, 80, 80) → y in [32, 112), so it overlaps a y>=96
    # textbox. The overlap-free upper center (y+h <= 96) is unchanged.
    assert region_hash(overworld, (40, 32, 80, 64)) == region_hash(dialogue, (40, 32, 80, 64))
    assert hash_named_regions(dialogue, regions)["bottom"] != first["bottom"]
    assert hash_named_regions(dialogue, regions)["full"] != first["full"]


def test_scale_nearest_and_encode_png_magic() -> None:
    native = _solid((12, 34, 56))
    for scale in (1, 2, 3, 4):
        image = scale_nearest(native, scale)
        assert image.size == (NATIVE_WIDTH * scale, NATIVE_HEIGHT * scale)
        png = encode_png(image)
        assert png.startswith(PNG_MAGIC)
        loaded = PILImage.open(io.BytesIO(png))
        assert loaded.size[0] == NATIVE_WIDTH * scale


def test_screenshot_plan_keyframes_cap() -> None:
    frame = _solid((10, 20, 30))
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 40,
            "screenshot_mode": "keyframes",
            "screenshot_scale": 1,
            "disable_default_hold_abort": True,
        }
    )
    plan = ScreenshotPlan()
    for index in range(1, 41):
        plan.record(index, frame, final=(index == 40))
    packed = plan.package(play)
    assert len(packed["pngs"]) <= 5
    assert packed["screenshot_count"] == len(packed["pngs"])
    assert packed["screenshots_subsampled"] is False
    for png in packed["pngs"]:
        assert png.startswith(PNG_MAGIC)


def test_screenshot_plan_all_subsamples() -> None:
    frame = _solid((10, 20, 30))
    steps = [{"buttons": ["a"], "hold_frames": 1} for _ in range(40)]
    play = parse_play_input(
        {"steps": steps, "screenshot_mode": "all", "screenshot_scale": 1}
    )
    plan = ScreenshotPlan()
    for index in range(40):
        plan.record(index + 1, frame, final=(index == 39))
    packed = plan.package(play)
    assert len(packed["pngs"]) <= MAX_SCREENSHOT_ALL
    assert packed["screenshot_count"] == len(packed["pngs"])
    assert packed["screenshots_subsampled"] is True


def test_screenshot_plan_interrupt_and_final_same_frame() -> None:
    frame = _solid((10, 20, 30))
    play = parse_play_input(
        {
            "buttons": ["a"],
            "screenshot_mode": "interrupt_and_final",
            "screenshot_scale": 1,
        }
    )
    plan = ScreenshotPlan()
    plan.record(3, frame, interrupt=True, final=True)
    packed = plan.package(play)
    assert len(packed["pngs"]) == 1
    assert packed["screenshot_count"] == 1
    assert packed["screenshots"][0]["kind"] == "interrupt_and_final"
    assert packed["pngs"][0].startswith(PNG_MAGIC)


def test_battle_classifier_split_layout() -> None:
    overworld = _solid((80, 160, 80))
    assert classify(_battle())["battle_likely"] is True
    assert classify(overworld)["battle_likely"] is False


def test_start_menu_classifier_left_pane() -> None:
    assert classify(_start_menu())["start_menu_likely"] is True
    assert classify(_solid((80, 160, 80)))["start_menu_likely"] is False


def test_capture_native_drops_alpha_and_falls_back_to_image() -> None:
    class _NdarrayScreen:
        @property
        def ndarray(self) -> np.ndarray:
            rgba = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 4), dtype=np.uint8)
            rgba[..., 0] = 1
            rgba[..., 1] = 2
            rgba[..., 2] = 3
            rgba[..., 3] = 255
            return rgba

    class _ImageScreen:
        ndarray = None
        image = PILImage.new("RGB", (NATIVE_WIDTH, NATIVE_HEIGHT), (9, 8, 7))

    class _PyBoy:
        def __init__(self, screen: object) -> None:
            self.screen = screen

    from_nd = capture_native(_PyBoy(_NdarrayScreen()))
    assert from_nd.shape == (NATIVE_HEIGHT, NATIVE_WIDTH, 3)
    assert from_nd.dtype == np.uint8
    assert tuple(from_nd[0, 0]) == (1, 2, 3)

    from_im = capture_native(_PyBoy(_ImageScreen()))
    assert from_im.shape == (NATIVE_HEIGHT, NATIVE_WIDTH, 3)
    assert tuple(from_im[0, 0]) == (9, 8, 7)
