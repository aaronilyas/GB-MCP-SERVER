"""Framebuffer classifiers, until-eval, and screenshot packaging."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

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
    fight_cursor_cell,
    hash_named_regions,
    pixel_delta_fraction,
    player_moved_from_frames,
    region_hash,
    scale_nearest,
    textbox_complete,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> np.ndarray:
    image = PILImage.open(_FIXTURES / name).convert("RGB")
    arr = np.asarray(image, dtype=np.uint8)
    assert arr.shape[:2] == (NATIVE_HEIGHT, NATIVE_WIDTH)
    return arr


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


def _start_menu_right(base: tuple[int, int, int] = (80, 160, 80)) -> np.ndarray:
    """Gen 1 Start menu: light pane on the right half."""
    frame = _solid(base)
    frame[:, 80:, :] = (248, 248, 248)
    return frame


def _pallet_like_overworld() -> np.ndarray:
    """Mid-green field, darker tree belt, lighter pavement, thin ledges."""
    frame = _solid((80, 160, 80))
    frame[:40, :, :] = (24, 72, 24)
    frame[96:, :, :] = (176, 176, 152)
    frame[64:68, :, :] = (220, 220, 200)
    frame[80:83, :, :] = (210, 210, 190)
    return frame


def _route_like_grass() -> np.ndarray:
    """Two similar greens and no HP-bar strips."""
    frame = _solid((70, 150, 70))
    frame[72:, :, :] = (90, 170, 90)
    return frame


def _interior_floor() -> np.ndarray:
    return _solid((140, 100, 56))


def _room() -> np.ndarray:
    """Non-uniform interior-like playfield (checker floor + furniture)."""
    frame = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 3), dtype=np.uint8)
    rows, cols = np.indices((NATIVE_HEIGHT, NATIVE_WIDTH))
    checker = ((cols // 8) + (rows // 8)) % 2 == 0
    frame[checker] = (80, 160, 80)
    frame[~checker] = (48, 112, 48)
    frame[32:96, 24:136] = (140, 88, 40)
    frame[16:40, 16:56] = (200, 160, 80)
    return frame


def _stale_window_slab(kind: str) -> np.ndarray:
    frame = _room()
    if kind == "bottom":
        frame[96:, :, :] = (4, 4, 4)
    elif kind == "top":
        frame[:72, :, :] = (4, 4, 4)
    elif kind == "right":
        frame[:, 80:, :] = (4, 4, 4)
    else:
        raise ValueError(kind)
    return frame


def _fade_black() -> np.ndarray:
    return _solid((0, 0, 0))


def _overworld_field() -> np.ndarray:
    """Pallet-like grass with an 8×8 checker, tree belt, path, and a house."""
    frame = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 3), dtype=np.uint8)
    light = (88, 168, 72)
    dark = (56, 136, 56)
    for y in range(0, NATIVE_HEIGHT, 8):
        for x in range(0, NATIVE_WIDTH, 8):
            color = light if ((x // 8) + (y // 8)) % 2 == 0 else dark
            frame[y : y + 8, x : x + 8] = color
    frame[:, 40:56, :] = (24, 72, 24)
    frame[100:116, :, :] = (176, 144, 72)
    frame[24:48, 96:120, :] = (160, 48, 48)
    frame[32:48, 100:116, :] = (200, 180, 120)
    return frame


def _hold_play(**extra: object):
    payload: dict = {"macro": "hold", "buttons": ["up"], "max_frames": 200}
    payload.update(extra)
    return parse_play_input(payload)


def test_textbox_and_pixel_delta_on_dialogue_bar() -> None:
    green = _solid((32, 200, 32))
    dialogue = _dialogue_bar((32, 200, 32))
    assert classify(green)["textbox_likely"] is False
    assert classify(dialogue)["textbox_likely"] is True
    assert classify(dialogue)["battle_likely"] is False
    assert pixel_delta_fraction(green, dialogue, DEFAULT_REGION) > 0.08


def test_until_monitor_default_hold_abort() -> None:
    play = _hold_play()
    assert play.apply_default_hold_abort is True
    overworld = _overworld_field()
    monitor = UntilMonitor(play, overworld)
    decision = monitor.evaluate(_battle(), 0)
    assert decision is not None
    assert decision.reason == "default_hold_abort"
    assert decision.until_fired is True


def test_until_monitor_default_hold_abort_on_fade() -> None:
    play = _hold_play()
    overworld = _overworld_field()
    monitor = UntilMonitor(play, overworld)
    black = monitor.evaluate(_solid((0, 0, 0)), 0)
    assert black is not None
    assert black.reason == "default_hold_abort"
    white = UntilMonitor(play, overworld).evaluate(_solid((255, 255, 255)), 0)
    assert white is not None
    assert white.reason == "default_hold_abort"


def test_until_monitor_default_hold_abort_ignores_overworld_scroll() -> None:
    play = _hold_play()
    base = _overworld_field()
    monitor = UntilMonitor(play, base)
    scrolled = np.roll(base, 8, axis=0)
    assert pixel_delta_fraction(base, scrolled, DEFAULT_REGION) > 0.12
    assert classify(base)["battle_likely"] is False
    assert classify(scrolled)["battle_likely"] is False
    for shift in range(1, 33):
        frame = np.roll(base, shift, axis=0)
        assert monitor.evaluate(frame, shift) is None


def test_until_monitor_default_hold_abort_on_start_menu() -> None:
    play = _hold_play()
    overworld = _overworld_field()
    assert classify(overworld)["start_menu_likely"] is False
    menu = _start_menu()
    assert classify(menu)["start_menu_likely"] is True
    decision = UntilMonitor(play, overworld).evaluate(menu, 0)
    assert decision is not None
    assert decision.reason == "default_hold_abort"
    right = _start_menu_right()
    assert classify(right)["start_menu_likely"] is True
    right_decision = UntilMonitor(play, overworld).evaluate(right, 0)
    assert right_decision is not None
    assert right_decision.reason == "default_hold_abort"


def test_until_monitor_disable_default_hold_abort() -> None:
    play = _hold_play(disable_default_hold_abort=True)
    assert play.apply_default_hold_abort is False
    monitor = UntilMonitor(play, _overworld_field())
    assert monitor.evaluate(_battle(), 0) is None
    assert monitor.evaluate(_solid((0, 0, 0)), 0) is None
    assert monitor.evaluate(_start_menu(), 0) is None


def test_until_monitor_caller_until_wins_over_default_abort() -> None:
    play = _hold_play(until={"on": "pixel_delta_above", "threshold": 0.08})
    assert play.apply_default_hold_abort is True
    monitor = UntilMonitor(play, _overworld_field())
    decision = monitor.evaluate(_battle(), 0)
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


def test_screenshot_plan_keeps_native_pngs_when_preview_is_scaled() -> None:
    frame = _solid((12, 34, 56))
    play = parse_play_input(
        {"buttons": ["a"], "screenshot_mode": "final", "screenshot_scale": 4}
    )
    plan = ScreenshotPlan()
    plan.record(1, frame, final=True)
    packed = plan.package(play)
    assert len(packed["pngs"]) == 1
    assert len(packed["pngs_native"]) == 1
    preview = PILImage.open(io.BytesIO(packed["pngs"][0]))
    native = PILImage.open(io.BytesIO(packed["pngs_native"][0]))
    assert preview.size == (NATIVE_WIDTH * 4, NATIVE_HEIGHT * 4)
    assert native.size == (NATIVE_WIDTH, NATIVE_HEIGHT)
    assert packed["pngs_native"][0].startswith(PNG_MAGIC)


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
    battle = classify(_battle())
    assert battle["battle_likely"] is True
    assert battle["textbox_likely"] is False
    assert battle["start_menu_likely"] is False
    assert classify(_solid((80, 160, 80)))["battle_likely"] is False
    assert classify(_pallet_like_overworld())["battle_likely"] is False
    assert classify(_route_like_grass())["battle_likely"] is False
    assert classify(_interior_floor())["battle_likely"] is False


def test_start_menu_classifier_left_pane() -> None:
    flags = classify(_start_menu())
    assert flags["start_menu_likely"] is True
    assert flags["battle_likely"] is False
    assert classify(_solid((80, 160, 80)))["start_menu_likely"] is False


def test_start_menu_classifier_right_pane() -> None:
    flags = classify(_start_menu_right())
    assert flags["start_menu_likely"] is True
    assert flags["battle_likely"] is False
    assert flags["textbox_likely"] is False


def test_live_pallet_overworld_is_not_a_battle() -> None:
    """Captured Pallet pavement LCD must not trip battle_likely (2026-09-05)."""
    frame = _load_fixture("pallet_overworld.png")
    flags = classify(frame)
    assert flags["battle_likely"] is False
    assert flags["textbox_likely"] is False
    assert flags["start_menu_likely"] is False
    assert flags["window_occluded_likely"] is False


def test_pallet_overworld_is_not_battle_or_menu() -> None:
    """Pallet trees, pavement, and house windows must not look like battle or Start."""
    from gb_mcp.emulator.loop import shape_public_status

    for name in ("pallet_overworld.png", "lcd/pallet_like.png", "lcd/fence_like.png", "lcd/grass_like.png"):
        flags = classify(_load_fixture(name))
        assert flags["battle_likely"] is False, name
        assert flags["start_menu_likely"] is False, name
        public = shape_public_status({"classifiers": flags, "running": True})
        assert "looks_like" not in public, name


def test_building_facade_is_not_start_menu() -> None:
    """A brick/house wall is not a Start menu even when one half is lighter."""
    frame = _solid((80, 160, 80))
    frame[:, 80:] = (210, 190, 170)
    frame[:18, 80:] = (120, 64, 40)
    for y in (24, 56, 88):
        for x in (92, 124):
            frame[y : y + 14, x : x + 16] = (80, 140, 200)
    frame[110:144, 112:132] = (70, 40, 24)
    flags = classify(frame)
    assert flags["start_menu_likely"] is False
    assert flags["battle_likely"] is False


def test_dark_rug_is_not_looks_like_fade() -> None:
    """A furnished room with a dark rug is not fade / window occlusion."""
    from gb_mcp.emulator.loop import shape_public_status

    frame = _room()
    frame[78:118, 36:124] = (18, 14, 10)
    flags = classify(frame)
    assert flags["window_occluded_likely"] is False
    public = shape_public_status({"classifiers": flags, "running": True})
    assert public.get("looks_like") != "fade"
    assert "looks_like" not in public


def test_live_start_menu_right_pane_from_pallet() -> None:
    """Captured Gen 1 Start menu (POKéDEX first, right-hand pane)."""
    frame = _load_fixture("start_menu_right.png")
    flags = classify(frame)
    assert flags["start_menu_likely"] is True
    assert flags["battle_likely"] is False
    assert flags["textbox_likely"] is False


def test_hold_abort_ignores_scroll_on_live_pallet() -> None:
    play = _hold_play()
    base = _load_fixture("pallet_overworld.png")
    monitor = UntilMonitor(play, base)
    scrolled = np.roll(base, 8, axis=0)
    assert pixel_delta_fraction(base, scrolled, DEFAULT_REGION) > 0.12
    assert classify(base)["battle_likely"] is False
    assert classify(scrolled)["battle_likely"] is False
    assert monitor.evaluate(scrolled, 0) is None


def test_capture_native_drops_alpha_and_falls_back_to_image() -> None:
    class _NdarrayScreen:
        def __init__(self) -> None:
            rgba = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 4), dtype=np.uint8)
            rgba[..., 0] = 1
            rgba[..., 1] = 2
            rgba[..., 2] = 3
            rgba[..., 3] = 255
            self.ndarray = rgba

    class _ImageScreen:
        ndarray = None
        image = PILImage.new("RGB", (NATIVE_WIDTH, NATIVE_HEIGHT), (9, 8, 7))

    class _PyBoy:
        def __init__(self, screen: object) -> None:
            self.screen = screen

    nd_screen = _NdarrayScreen()
    from_nd = capture_native(_PyBoy(nd_screen))
    assert from_nd.shape == (NATIVE_HEIGHT, NATIVE_WIDTH, 3)
    assert from_nd.dtype == np.uint8
    assert from_nd.flags["C_CONTIGUOUS"]
    assert tuple(from_nd[0, 0]) == (1, 2, 3)
    nd_screen.ndarray[..., :3] = 99
    assert tuple(from_nd[0, 0]) == (1, 2, 3)

    from_im = capture_native(_PyBoy(_ImageScreen()))
    assert from_im.shape == (NATIVE_HEIGHT, NATIVE_WIDTH, 3)
    assert from_im.flags["C_CONTIGUOUS"]
    assert tuple(from_im[0, 0]) == (9, 8, 7)

    class _AliasedImageScreen:
        ndarray = None

        def __init__(self) -> None:
            self.buf = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 4), dtype=np.uint8)
            self.buf[..., 0] = 9
            self.buf[..., 1] = 8
            self.buf[..., 2] = 7
            self.buf[..., 3] = 255
            self.image = PILImage.frombuffer(
                "RGBA",
                (NATIVE_WIDTH, NATIVE_HEIGHT),
                self.buf,
                "raw",
                "RGBA",
                0,
                1,
            )

    aliased = _AliasedImageScreen()
    from_alias = capture_native(_PyBoy(aliased))
    assert from_alias.shape == (NATIVE_HEIGHT, NATIVE_WIDTH, 3)
    assert from_alias.flags["C_CONTIGUOUS"]
    assert tuple(from_alias[0, 0]) == (9, 8, 7)
    aliased.buf[..., :3] = 1
    assert tuple(from_alias[0, 0]) == (9, 8, 7)


def test_window_occluded_slab_not_textbox_or_fade() -> None:
    overworld = _room()
    dialogue = _dialogue_bar((80, 160, 80))
    fade = _fade_black()
    bottom = _stale_window_slab("bottom")
    top = _stale_window_slab("top")
    right = _stale_window_slab("right")

    ow = classify(overworld)
    assert ow["window_occluded_likely"] is False
    assert ow["textbox_likely"] is False

    box = classify(dialogue)
    assert box["textbox_likely"] is True
    assert box["window_occluded_likely"] is False

    faded = classify(fade)
    assert faded["window_occluded_likely"] is False
    assert faded["textbox_likely"] is False

    for slab in (bottom, top, right):
        flags = classify(slab)
        assert flags["window_occluded_likely"] is True
        assert flags["textbox_likely"] is False


def test_lcd_fixtures_battle_likely_overworld_vs_fight() -> None:
    pallet = _load_fixture("lcd/pallet_like.png")
    fence = _load_fixture("lcd/fence_like.png")
    grass = _load_fixture("lcd/grass_like.png")
    fight = _load_fixture("lcd/fight_hud.png")
    assert classify(pallet)["battle_likely"] is False
    assert classify(fence)["battle_likely"] is False
    assert classify(grass)["battle_likely"] is False
    assert classify(_pallet_like_overworld())["battle_likely"] is False
    flags = classify(fight)
    assert flags["battle_likely"] is True
    assert flags["textbox_likely"] is False
    assert fight_cursor_cell(fight) == "fight"


def test_textbox_complete_triangle() -> None:
    empty = _dialogue_bar()
    assert classify(empty)["textbox_likely"] is True
    assert textbox_complete(empty) is False
    prompt = _load_fixture("lcd/textbox_prompt.png")
    assert classify(prompt)["textbox_likely"] is True
    assert textbox_complete(prompt) is True


def test_public_fade_is_luma_jump_not_camera_scroll() -> None:
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 40,
            "until": {"on": "luma_jump"},
            "disable_default_hold_abort": True,
        }
    )
    base = _overworld_field()
    monitor = UntilMonitor(play, base)
    for shift in range(1, 17):
        frame = np.roll(base, shift, axis=0)
        assert monitor.evaluate(frame, shift) is None
    black = UntilMonitor(play, base).evaluate(_solid((0, 0, 0)), 0)
    assert black is not None
    assert black.reason == "fade"
    assert black.detail == "fade"
    assert player_moved_from_frames(base, np.roll(base, 8, axis=0)) is True
    assert player_moved_from_frames(base, base.copy()) is False


def test_blocked_crop_still_and_scroll_is_not_blocked() -> None:
    wall = _load_fixture("lcd/blocked_wall.png")
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 80,
            "until": {"on": "blocked", "stable_frames": 3},
            "until_eval_interval": 1,
            "disable_default_hold_abort": True,
        }
    )
    monitor = UntilMonitor(play, wall)
    decision = None
    for index in range(6):
        decision = monitor.evaluate(wall, index)
        if decision is not None:
            break
    assert decision is not None
    assert decision.reason == "blocked"

    field = _overworld_field()
    scroll_play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 40,
            "until": {"on": "blocked", "stable_frames": 3},
            "disable_default_hold_abort": True,
        }
    )
    scrolling = UntilMonitor(scroll_play, field)
    for shift in range(1, 12):
        assert scrolling.evaluate(np.roll(field, shift, axis=0), shift) is None


def test_until_textbox_disappears_does_not_fire_while_box_is_up() -> None:
    play = parse_play_input(
        {
            "buttons": ["a"],
            "hold_frames": 40,
            "until": {
                "on": "classifier",
                "classifier": "textbox_likely",
                "classifier_polarity": "disappears",
            },
        }
    )
    box = _dialogue_bar()
    field = _overworld_field()
    monitor = UntilMonitor(play, box)
    assert classify(box)["textbox_likely"] is True
    assert monitor.evaluate(box, 0) is None
    assert monitor.evaluate(box, 1) is None
    gone = monitor.evaluate(field, 2)
    assert gone is not None
    assert gone.reason == "classifier"
    assert gone.detail == "textbox"


def test_default_hold_abort_blocked_on_static_wall() -> None:
    wall = _load_fixture("lcd/blocked_wall.png")
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 80,
            "until_eval_interval": 1,
        }
    )
    assert play.apply_default_hold_abort is True
    monitor = UntilMonitor(play, wall)
    decision = None
    for index in range(24):
        decision = monitor.evaluate(wall, index)
        if decision is not None:
            break
    assert decision is not None
    assert decision.reason == "default_hold_abort"
    assert decision.detail == "blocked"


def _player_bob(base: np.ndarray, phase: int) -> np.ndarray:
    """16×16 walk-cycle bob in the player crop; rest of the LCD is still."""
    frame = base.copy()
    y = 72 + (phase % 2) * 2
    x = 72
    frame[y : y + 16, x : x + 16] = (200, 72, 72)
    return frame


def test_walk_cycle_center_crop_blocks_after_grace() -> None:
    base = _overworld_field()
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 80,
            "until_eval_interval": 1,
        }
    )
    monitor = UntilMonitor(play, base)
    decision = None
    for index in range(24):
        decision = monitor.evaluate(_player_bob(base, index), index)
        if decision is not None:
            break
    assert decision is not None
    assert decision.reason == "default_hold_abort"
    assert decision.detail == "blocked"
    # Facing turn / first evals must not abort.
    early = UntilMonitor(play, base)
    assert early.evaluate(_player_bob(base, 0), 0) is None
    assert early.evaluate(_player_bob(base, 1), 1) is None


def test_npc_in_corner_does_not_prevent_blocked() -> None:
    base = _overworld_field()
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 80,
            "until_eval_interval": 1,
        }
    )
    monitor = UntilMonitor(play, base)
    decision = None
    for index in range(24):
        frame = _player_bob(base, 0)
        x = 4 + (index * 3) % 24
        frame[4:20, x : x + 16] = (248, 248, 40)
        decision = monitor.evaluate(frame, index)
        if decision is not None:
            break
    assert decision is not None
    assert decision.detail == "blocked"


def test_full_screen_scroll_is_not_blocked() -> None:
    field = _overworld_field()
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 40,
            "until_eval_interval": 1,
        }
    )
    monitor = UntilMonitor(play, field)
    for shift in range(1, 20):
        decision = monitor.evaluate(np.roll(field, shift, axis=0), shift)
        assert decision is None


def test_battle_takeover_is_battle_not_blocked() -> None:
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 80,
            "until_eval_interval": 1,
        }
    )
    monitor = UntilMonitor(play, _overworld_field())
    decision = monitor.evaluate(_battle(), 0)
    assert decision is not None
    assert decision.reason == "default_hold_abort"
    assert decision.detail == "battle"


def test_screenshot_plan_keyframes_keep_interrupt() -> None:
    start = _overworld_field()
    battle = _battle()
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
    plan = ScreenshotPlan(play)
    plan.record(8, start, interrupt=False, final=False)
    plan.record(12, battle, interrupt=True, final=True)
    packed = plan.package(play)
    kinds = [item["kind"] for item in packed["screenshots"]]
    assert any("interrupt" in kind for kind in kinds)
    assert len(packed["pngs_native"]) >= 1
