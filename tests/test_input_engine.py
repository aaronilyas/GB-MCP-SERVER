"""Unit tests for the PyBoy tick + button scheduler."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import FakePyBoy
from gb_mcp.emulator.input_engine import run_play_input
from gb_mcp.emulator.input_schema import parse_play_input


class NullMonitor:
    def evaluate(self, frame, eval_index):
        return None


class AbortAfter:
    def __init__(self, n):
        self.n = n

    def evaluate(self, frame, eval_index):
        if eval_index >= self.n:
            return type("D", (), {"reason": "default_hold_abort", "until_fired": True})()
        return None


class CollectPlan:
    def __init__(self):
        self.records = []

    def want_render(self, frame_index, planned):
        return False

    def record(self, frame_index, frame, *, interrupt=False, final=False, **_kwargs):
        self.records.append((frame_index, interrupt, final))

    def package(self, play):
        return {
            "pngs": [],
            "screenshots": [],
            "screenshot_count": 0,
            "screenshots_subsampled": False,
        }


def _dummy_frame():
    return np.zeros((144, 160, 3), dtype=np.uint8)


def _run(pyboy, play, *, until=None, plan=None, monotonic=None):
    kwargs = {}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return run_play_input(
        pyboy,
        play,
        capture_native=_dummy_frame,
        until_monitor=until if until is not None else NullMonitor(),
        screenshot_plan=plan if plan is not None else CollectPlan(),
        **kwargs,
    )


def test_hold_up_200_frames_uncapped() -> None:
    """Hold Up 200 frames. Engine releases without an extra latch tick."""
    pyboy = FakePyBoy(Path("dummy.gb"))
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "hold_frames": 200,
            "emulation_speed": 0,
            "disable_default_hold_abort": True,
        }
    )
    result = _run(pyboy, play)
    assert result["frames_advanced"] == 200
    assert result["emulation_speed"] == 0
    assert result["macro"] == "hold"
    assert result["stop_reason"] == "max_frames"
    assert result["until_fired"] is False
    assert pyboy.speed == 0
    assert "up" in pyboy.presses
    assert pyboy.buttons == []
    assert pyboy._pressed == set()


def test_mash_a_press_release_cycle() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    play = parse_play_input(
        {
            "macro": "mash",
            "mash_button": "a",
            "mash_press_frames": 4,
            "mash_release_frames": 4,
            "max_frames": 80,
        }
    )
    result = _run(pyboy, play)
    assert result["frames_advanced"] == pytest.approx(80, abs=1)
    assert result["stop_reason"] == "max_frames"
    assert result["macro"] == "mash"
    assert pyboy.presses.count("a") >= 8
    assert pyboy.releases.count("a") >= 8
    assert pyboy._pressed == set()


def test_steps_script_wait_and_gap_frames() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    steps = [
        {"buttons": ["a"], "hold_frames": 2, "gap_frames": 2},
        {"buttons": ["b"], "hold_frames": 2, "gap_frames": 2},
        {"buttons": ["a"], "hold_frames": 2, "gap_frames": 2},
        {"buttons": ["b"], "hold_frames": 2, "gap_frames": 2},
        {"buttons": ["a"], "hold_frames": 2, "gap_frames": 2},
        {"buttons": ["b"], "hold_frames": 2, "gap_frames": 2},
        {"buttons": ["a"], "hold_frames": 2, "gap_frames": 2},
        {"buttons": ["b"], "hold_frames": 2, "gap_frames": 2},
        {"buttons": [], "hold_frames": 4, "gap_frames": 2},
        {"buttons": ["start"], "hold_frames": 3, "gap_frames": 0},
    ]
    expected = sum(step["hold_frames"] + step["gap_frames"] for step in steps)
    wait_index = 8
    wait_start = sum(
        step["hold_frames"] + step["gap_frames"] for step in steps[:wait_index]
    )
    wait_end = wait_start + steps[wait_index]["hold_frames"]
    observed: list[set[str]] = []
    orig_tick = pyboy.tick

    def wrapped(count: int = 1, render: bool = True, sound: bool = True) -> bool:
        start = pyboy.ticks
        n = count if isinstance(count, int) and count > 0 else 0
        if start < wait_end and start + n > wait_start:
            observed.append(set(pyboy._pressed))
        return orig_tick(count, render, sound)

    pyboy.tick = wrapped  # type: ignore[method-assign]
    play = parse_play_input({"steps": steps})
    result = _run(pyboy, play)
    assert result["frames_advanced"] == expected
    assert result["stop_reason"] == "completed"
    assert observed
    assert all(pressed == set() for pressed in observed)
    assert pyboy._pressed == set()
    assert any(step["buttons"] == [] for step in result["steps"])


def test_until_interrupt_releases_buttons_and_records() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 200,
            "until_eval_interval": 4,
            "disable_default_hold_abort": True,
        }
    )
    plan = CollectPlan()
    result = _run(pyboy, play, until=AbortAfter(0), plan=plan)
    assert result["stop_reason"] == "default_hold_abort"
    assert result["until_fired"] is True
    assert result["frames_advanced"] == 4
    assert pyboy._pressed == set()
    assert any(interrupt and final for _, interrupt, final in plan.records)


def test_render_only_on_capture_or_until_eval() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 80,
            "emulation_speed": 0,
            "until_eval_interval": 4,
            "disable_default_hold_abort": True,
        }
    )
    result = _run(pyboy, play)
    assert result["frames_advanced"] == 80
    assert any(count > 1 for count in pyboy.tick_calls)
    false_renders = pyboy.tick_renders.count(False)
    true_renders = pyboy.tick_renders.count(True)
    assert false_renders >= 10
    assert true_renders >= 1
    assert pyboy.buttons == []


def test_call_timeout_releases_buttons() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["a"],
            "max_frames": 200,
            "disable_default_hold_abort": True,
            "call_timeout_seconds": 1,
        }
    )

    class JumpClock:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            if self.calls <= 1:
                return 0.0
            return 10_000.0

    result = _run(pyboy, play, monotonic=JumpClock())
    assert result["stop_reason"] == "call_timeout"
    assert result["until_fired"] is False
    assert pyboy._pressed == set()
