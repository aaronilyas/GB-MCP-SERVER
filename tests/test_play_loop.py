"""Integration skeleton for the screenshot-only play loop (AGENT_CONTRACT).

These tests are expected to fail until sub-agents A/B/C land and the lead
wires `_apply_input`. They encode the 12 acceptance cases.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image as PILImage

import db
from gb_mcp.emulator.input_schema import parse_play_input
from gb_mcp.emulator.play_limits import (
    FORBIDDEN_RESPONSE_KEY_NEEDLES,
    MAX_SCREENSHOT_ALL,
    NATIVE_HEIGHT,
    NATIVE_WIDTH,
    SEND_INPUT_RESPONSE_KEYS,
)
from gb_mcp.emulator.session import SessionManager
from rom_builder import make_rom

from conftest import FakePyBoy

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _mapped_rom(roms_dir: Path, *, email: str = "owner@example.com", name: str | None = None) -> tuple[str, Path]:
    name = name or ("a" * db.SUBDIRECTORY_NAME_LENGTH)
    dest = roms_dir / name
    dest.mkdir()
    rom_path = dest / "tetris.gb"
    rom_path.write_bytes(make_rom(title=b"TETRIS"))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, email)
    return name, rom_path


def _solid(color: tuple[int, int, int]) -> np.ndarray:
    frame = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 3), dtype=np.uint8)
    frame[:, :] = color
    return frame


def _dialogue_bar(base: tuple[int, int, int] = (80, 160, 80)) -> np.ndarray:
    """Overworld-ish field plus a Gen 1-like bottom window."""
    frame = _solid(base)
    frame[96:144, :, :] = (16, 16, 16)
    frame[100:140, 8:152, :] = (248, 248, 248)
    return frame


def _flatten_keys(payload: object, prefix: str = "") -> list[str]:
    keys: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.append(path)
            keys.extend(_flatten_keys(value, path))
    elif isinstance(payload, list):
        for item in payload[:8]:
            keys.extend(_flatten_keys(item, prefix))
    return keys


def test_schema_empty_wait_and_caps() -> None:
    """Case 1: >500 steps rejected; hold 3601 rejected; both modes rejected; wait ok."""
    from gb_mcp.emulator.play_limits import MAX_HOLD_FRAMES, MAX_INPUT_STEPS

    with pytest.raises(ValueError, match="steps cannot exceed"):
        parse_play_input({"steps": [{"buttons": ["a"]}] * (MAX_INPUT_STEPS + 1)})
    with pytest.raises(ValueError, match="hold_frames"):
        parse_play_input({"buttons": ["a"], "hold_frames": 3601})
    with pytest.raises(ValueError, match="not both"):
        parse_play_input({"buttons": ["a"], "steps": [{"buttons": ["b"]}]})
    play = parse_play_input({"steps": [{"buttons": [], "hold_frames": 5}]})
    assert play.steps[0].wait is True
    assert MAX_HOLD_FRAMES == 3600
    assert MAX_INPUT_STEPS == 500


def test_speed_uncapped_batch_is_fast() -> None:
    """Case 2: 120 uncapped frames well under 2s process time."""
    from gb_mcp.emulator.input_engine import run_play_input
    from gb_mcp.emulator.vision import (
        ScreenshotPlan,
        UntilMonitor,
        capture_native,
    )

    pyboy = FakePyBoy(Path("dummy.gb"))
    pyboy.set_emulation_speed(0)
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 120,
            "emulation_speed": 0,
            "disable_default_hold_abort": True,
            "screenshot_mode": "final",
        }
    )
    baseline = capture_native(pyboy)
    t0 = time.perf_counter()
    result = run_play_input(
        pyboy,
        play,
        capture_native=lambda: capture_native(pyboy),
        until_monitor=UntilMonitor(play, baseline),
        screenshot_plan=ScreenshotPlan(),
    )
    elapsed = time.perf_counter() - t0
    assert result["frames_advanced"] >= 120
    assert elapsed < 2.0
    assert pyboy.speed == 0


def test_hold_default_abort_on_screen_change() -> None:
    """Case 3: still frame then a very different frame aborts hold; buttons released."""
    from gb_mcp.emulator.input_engine import run_play_input
    from gb_mcp.emulator.vision import ScreenshotPlan, UntilMonitor, capture_native

    pyboy = FakePyBoy(Path("dummy.gb"))

    def factory(ticks: int, _pressed: set[str]) -> PILImage.Image:
        if ticks < 20:
            return PILImage.fromarray(_solid((32, 160, 32)))
        return PILImage.fromarray(_solid((200, 16, 16)))

    pyboy.frame_factory = factory
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 200,
            "until_eval_interval": 1,
            "screenshot_mode": "interrupt_and_final",
        }
    )
    baseline = capture_native(pyboy)
    result = run_play_input(
        pyboy,
        play,
        capture_native=lambda: capture_native(pyboy),
        until_monitor=UntilMonitor(play, baseline),
        screenshot_plan=ScreenshotPlan(),
    )
    assert result["stop_reason"] in {"default_hold_abort", "screen_change"}
    assert result["until_fired"] is True
    assert result["frames_advanced"] < 200
    assert pyboy._pressed == set()


def test_mash_a_advances_max_frames() -> None:
    """Case 4: A pressed and released multiple times; frames_advanced ≈ max_frames."""
    from gb_mcp.emulator.input_engine import run_play_input
    from gb_mcp.emulator.vision import ScreenshotPlan, UntilMonitor, capture_native

    pyboy = FakePyBoy(Path("dummy.gb"))
    play = parse_play_input(
        {
            "macro": "mash",
            "mash_button": "a",
            "mash_press_frames": 4,
            "mash_release_frames": 4,
            "max_frames": 80,
            "screenshot_mode": "final",
        }
    )
    baseline = capture_native(pyboy)
    result = run_play_input(
        pyboy,
        play,
        capture_native=lambda: capture_native(pyboy),
        until_monitor=UntilMonitor(play, baseline),
        screenshot_plan=ScreenshotPlan(),
    )
    assert result["frames_advanced"] == pytest.approx(80, abs=2)
    assert pyboy.presses.count("a") >= 8
    assert pyboy.releases.count("a") >= 8
    assert pyboy._pressed == set()


def test_wait_and_gap_release_buttons() -> None:
    """Case 5: 3-step script with wait + gap ticks with no buttons down during wait/gap."""
    from gb_mcp.emulator.input_engine import run_play_input
    from gb_mcp.emulator.vision import ScreenshotPlan, UntilMonitor, capture_native

    pyboy = FakePyBoy(Path("dummy.gb"))
    pressed_during_wait: list[set[str]] = []

    orig_tick = pyboy.tick

    def wrapped(count: int = 1, render: bool = True, sound: bool = True) -> bool:
        # After the first chord, wait step should have nothing down.
        if pyboy.ticks >= 4:
            pressed_during_wait.append(set(pyboy._pressed))
        return orig_tick(count, render, sound)

    pyboy.tick = wrapped  # type: ignore[method-assign]
    play = parse_play_input(
        {
            "steps": [
                {"buttons": ["up"], "hold_frames": 4, "gap_frames": 2},
                {"buttons": [], "hold_frames": 6},
                {"buttons": ["a"], "hold_frames": 2, "gap_frames": 1},
            ]
        }
    )
    baseline = capture_native(pyboy)
    result = run_play_input(
        pyboy,
        play,
        capture_native=lambda: capture_native(pyboy),
        until_monitor=UntilMonitor(play, baseline),
        screenshot_plan=ScreenshotPlan(),
    )
    assert result["frames_advanced"] >= 4 + 2 + 6 + 2 + 1
    # During/after the wait portion, buttons must be empty at least once.
    assert any(pressed == set() for pressed in pressed_during_wait)
    assert pyboy._pressed == set()


def test_screenshot_modes_counts() -> None:
    """Case 6: interrupt_and_final 1–2 PNGs; keyframes ≤5; all with 40 steps ≤30 + flag."""
    from gb_mcp.emulator.vision import ScreenshotPlan

    frame = _solid((10, 20, 30))
    interrupt = parse_play_input(
        {"buttons": ["a"], "screenshot_mode": "interrupt_and_final", "screenshot_scale": 1}
    )
    plan = ScreenshotPlan()
    plan.record(3, frame, interrupt=True, final=True)
    packed = plan.package(interrupt)
    assert 1 <= len(packed["pngs"]) <= 2
    assert packed["screenshot_count"] == len(packed["pngs"])
    for png in packed["pngs"]:
        assert png.startswith(PNG_MAGIC)

    key_play = parse_play_input(
        {"macro": "hold", "buttons": ["up"], "max_frames": 40, "screenshot_mode": "keyframes", "screenshot_scale": 1, "disable_default_hold_abort": True}
    )
    plan = ScreenshotPlan()
    for i in range(1, 41):
        plan.record(i, frame, final=(i == 40))
    packed = plan.package(key_play)
    assert 1 <= len(packed["pngs"]) <= 5

    steps = [{"buttons": ["a"], "hold_frames": 1} for _ in range(40)]
    all_play = parse_play_input({"steps": steps, "screenshot_mode": "all", "screenshot_scale": 1})
    plan = ScreenshotPlan()
    for i in range(40):
        plan.record(i + 1, frame, final=(i == 39))
    packed = plan.package(all_play)
    assert len(packed["pngs"]) <= MAX_SCREENSHOT_ALL
    assert packed.get("screenshots_subsampled") is True


def test_screenshot_scale_width() -> None:
    """Case 7: returned PNG width is 160 * screenshot_scale."""
    from gb_mcp.emulator.vision import encode_png, scale_nearest

    native = _solid((12, 34, 56))
    for scale in (1, 2, 3, 4):
        image = scale_nearest(native, scale)
        png = encode_png(image)
        loaded = PILImage.open(io.BytesIO(png))
        assert loaded.size == (NATIVE_WIDTH * scale, NATIVE_HEIGHT * scale)


def test_region_hashes_identical_and_dialogue_bottom() -> None:
    """Case 8: identical frames share full hash; dialogue bar changes bottom more than center."""
    from gb_mcp.emulator.play_limits import BOTTOM_REGION, CENTER_REGION, DEFAULT_REGION
    from gb_mcp.emulator.vision import pixel_delta_fraction, region_hash

    overworld = _solid((80, 160, 80))
    assert region_hash(overworld, DEFAULT_REGION) == region_hash(overworld.copy(), DEFAULT_REGION)
    dialogue = _dialogue_bar((80, 160, 80))
    assert region_hash(overworld, BOTTOM_REGION) != region_hash(dialogue, BOTTOM_REGION)
    # Named center [40,32,80,80] overlaps the top of the dialogue band (y>=96)
    # by 16 rows; the bottom box must still change more than center.
    assert pixel_delta_fraction(overworld, dialogue, BOTTOM_REGION) > pixel_delta_fraction(
        overworld, dialogue, CENTER_REGION
    )


def test_ping_does_not_tick_and_resets_idle(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    """Case 9: ping does not change frame count; resets idle timestamp."""
    name, rom_path = _mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy
    ticks_before = pyboy.ticks
    remaining_before = session.seconds_until_idle_close()
    time.sleep(0.05)
    result = pyboy_manager.ping("owner@example.com", name)
    assert result["alive"] is True
    assert pyboy.ticks == ticks_before
    assert result["seconds_since_last_input"] < 0.05
    assert session.seconds_until_idle_close() >= remaining_before - 0.01


def test_save_battery_writes_and_keeps_session(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    """Case 10: save file mtime/size changes; session still accepts input."""
    name, rom_path = _mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    state = Path(str(rom_path) + ".state")
    assert not state.exists() or state.stat().st_size == 0
    result = pyboy_manager.save_battery("owner@example.com", name)
    assert result["saved"] is True
    assert state.is_file() and state.stat().st_size > 0
    mtime = state.stat().st_mtime
    time.sleep(0.05)
    again = pyboy_manager.save_battery("owner@example.com", name)
    assert again["saved"] is True
    assert state.stat().st_mtime >= mtime
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    assert session.is_running is True
    sent = pyboy_manager.send_input("owner@example.com", name, ["a"])
    assert sent.get("sent") is True


def test_no_game_state_leakage_allowlist() -> None:
    """Case 11: response keys allowlist; forbidden needles fail CI."""
    sample = {
        "sent": True,
        "stop_reason": "completed",
        "frames_advanced": 4,
        "emulation_speed": 0,
        "until_fired": False,
        "region_hashes": {"full": "abc", "bottom": "def", "center": "ghi"},
        "classifiers": {
            "textbox_likely": False,
            "battle_likely": False,
            "start_menu_likely": False,
        },
        "screenshot_scale": 4,
        "native_size": [160, 144],
        "email": "owner@example.com",
        "subdirectory": "a" * 32,
        "rom": "tetris.gb",
        "running": True,
        "saved": False,
        "screenshot_mode": "final",
        "screenshot_count": 1,
        "screenshots": [{"kind": "final", "frame_index": 4, "step_index": 0}],
        "macro": "buttons",
    }
    for path in _flatten_keys(sample):
        leaf = path.split(".")[-1].lower()
        assert path.split(".")[-1] in SEND_INPUT_RESPONSE_KEYS or leaf in {
            "full",
            "bottom",
            "center",
            "textbox_likely",
            "battle_likely",
            "start_menu_likely",
            "kind",
            "frame_index",
            "step_index",
        }
        joined = path.lower()
        for needle in FORBIDDEN_RESPONSE_KEY_NEEDLES:
            assert needle not in joined


def test_submit_email_maps_without_map_tool(fake_docker_submit, isolated_db, roms_dir: Path) -> None:
    """Case 12: submit + email creates the mapping without map_subdirectory_to_email."""
    import server
    from rom_builder import make_rom as _make

    result = server.submit_gb_rom(
        __import__("base64").b64encode(_make()).decode(),
        email="Owner@Example.com",
    )
    assert result["accepted"] is True
    assert result["mapped"] is True
    assert result["email"] == "owner@example.com"
    name = result["subdirectory"]
    with db.session_scope() as session:
        row = db.get_subdirectory_for_email(session, name, "owner@example.com")
    assert row is not None


@pytest.fixture
def fake_docker_submit(monkeypatch: pytest.MonkeyPatch):
    import server

    monkeypatch.setattr(server, "_docker_available", lambda: None)
    monkeypatch.setattr(server, "_ensure_image", lambda: None)
    monkeypatch.setattr(server, "_create_isolated_container", lambda: "cid")
    monkeypatch.setattr(server, "_destroy_container", lambda _cid: None)
    monkeypatch.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": True, "reason": "ok"},
    )
    return monkeypatch
