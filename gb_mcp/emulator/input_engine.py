"""PyBoy tick + button scheduler; input_schema and play_limits define its contract."""

from __future__ import annotations

import time
from typing import Any, NamedTuple

from gb_mcp.emulator.input_schema import PlayInput
from gb_mcp.emulator.play_limits import MAX_FRAMES_PER_CALL

# tick(n, render=False) can skip LCD work Gen 1 map scripts need.
_MAX_TICK_WITHOUT_RENDER = 4


class _Phase(NamedTuple):
    buttons: tuple[str, ...]
    frames: int
    step_index: int
    report: bool


def run_play_input(
    pyboy: Any,
    play: PlayInput,
    *,
    capture_native: Any,
    until_monitor: Any,
    screenshot_plan: Any,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    """Drive buttons and ticks. Return engine fields + screenshot_plan.package()."""
    clock = time.monotonic if monotonic is None else monotonic
    start = clock()
    timeout = float(play.call_timeout_seconds)
    interval = play.until_eval_interval
    planned = play.planned_frames
    budget = play.max_frames
    if budget > MAX_FRAMES_PER_CALL:
        budget = MAX_FRAMES_PER_CALL
    if interval < 1:
        interval = 1

    setter = getattr(pyboy, "set_emulation_speed", None)
    if setter is not None:
        setter(play.emulation_speed)

    phases = _phases_for(play, budget)
    total_frames = min(budget, sum(phase.frames for phase in phases))

    pressed: set[str] = set()
    steps_out: list[dict[str, Any]] = []
    frames_advanced = 0
    eval_index = 0
    stop_reason: str | None = None
    until_fired = False
    final_recorded = False
    phase_idx = 0
    phase_progress = 0
    chord_changed = False

    try:
        if phases:
            chord_changed = _set_buttons(pyboy, pressed, phases[0].buttons)
            _maybe_report_step(steps_out, phases[0])

        while frames_advanced < total_frames:
            if clock() - start >= timeout:
                stop_reason = "call_timeout"
                until_fired = False
                break

            phase = phases[phase_idx]
            phase_left = phase.frames - phase_progress
            next_eval = _next_eval_frame(frames_advanced, interval, total_frames)
            chunk = min(phase_left, next_eval - frames_advanced, total_frames - frames_advanced)
            if screenshot_plan is not None:
                for offset in range(1, chunk):
                    if screenshot_plan.want_render(frames_advanced + offset, planned):
                        chunk = offset
                        break
            if chunk <= 0:
                break

            end = frames_advanced + chunk
            is_last = end == total_frames
            need_eval = end == next_eval
            want = (
                screenshot_plan.want_render(end, planned)
                if screenshot_plan is not None
                else False
            )
            phase_will_end = phase_progress + chunk >= phase.frames
            step_shot = (
                play.screenshot_mode == "all" and phase_will_end and phase.report
            )
            need_render = need_eval or want or is_last or step_shot

            _tick_chunk(
                pyboy,
                chunk,
                render_last=need_render,
                render_first=chord_changed,
            )
            chord_changed = False
            frames_advanced = end
            phase_progress += chunk

            if need_render:
                frame = capture_native() if capture_native is not None else None
                if need_eval and until_monitor is not None:
                    decision = until_monitor.evaluate(frame, eval_index)
                    eval_index += 1
                    if decision is not None:
                        _release_all(pyboy, pressed)
                        if screenshot_plan is not None:
                            screenshot_plan.record(
                                frames_advanced,
                                frame,
                                interrupt=True,
                                final=True,
                                step_index=phase.step_index,
                            )
                        final_recorded = True
                        stop_reason = getattr(decision, "reason", "completed")
                        until_fired = bool(getattr(decision, "until_fired", True))
                        break
                if screenshot_plan is not None and (want or is_last or step_shot):
                    screenshot_plan.record(
                        frames_advanced,
                        frame,
                        interrupt=False,
                        final=is_last,
                        step_index=phase.step_index,
                    )
                    if is_last:
                        final_recorded = True

            if clock() - start >= timeout:
                stop_reason = "call_timeout"
                until_fired = False
                break

            if phase_progress >= phase.frames:
                phase_idx += 1
                phase_progress = 0
                if phase_idx < len(phases) and frames_advanced < total_frames:
                    chord_changed = _set_buttons(
                        pyboy, pressed, phases[phase_idx].buttons
                    )
                    _maybe_report_step(steps_out, phases[phase_idx])
    finally:
        _release_all(pyboy, pressed)

    if stop_reason is None:
        if play.macro in {"hold", "mash"} and frames_advanced >= budget:
            stop_reason = "max_frames"
        else:
            stop_reason = "completed"

    if not final_recorded and screenshot_plan is not None:
        frame = capture_native() if capture_native is not None else None
        screenshot_plan.record(
            frames_advanced, frame, interrupt=False, final=True
        )

    packed = screenshot_plan.package(play) if screenshot_plan is not None else {}
    engine = {
        "frames_advanced": frames_advanced,
        "stop_reason": stop_reason,
        "until_fired": until_fired,
        "emulation_speed": play.emulation_speed,
        "macro": play.macro,
        "steps": steps_out,
    }
    return {**packed, **engine}


def _phases_for(play: PlayInput, budget: int) -> list[_Phase]:
    raw: list[_Phase]
    if play.macro == "mash":
        raw = _mash_phases(play, budget)
    elif play.macro == "hold":
        raw = [_Phase(tuple(play.buttons), budget, 0, True)] if budget > 0 else []
    else:
        raw = []
        for index, step in enumerate(play.steps):
            if step.hold_frames > 0:
                raw.append(_Phase(tuple(step.buttons), step.hold_frames, index, True))
            if step.gap_frames > 0:
                raw.append(_Phase((), step.gap_frames, index, False))

    out: list[_Phase] = []
    used = 0
    for phase in raw:
        if used >= budget:
            break
        frames = phase.frames
        if used + frames > budget:
            frames = budget - used
        if frames <= 0:
            continue
        out.append(_Phase(phase.buttons, frames, phase.step_index, phase.report))
        used += frames
    return out


def _mash_phases(play: PlayInput, budget: int) -> list[_Phase]:
    press = play.mash_press_frames
    release = play.mash_release_frames
    button = (play.mash_button,)
    phases: list[_Phase] = []
    remaining = budget
    index = 0
    while remaining > 0:
        held = min(press, remaining)
        phases.append(_Phase(button, held, index, True))
        index += 1
        remaining -= held
        if remaining <= 0:
            break
        up = min(release, remaining)
        phases.append(_Phase((), up, index, True))
        index += 1
        remaining -= up
    return phases


def _next_eval_frame(current: int, interval: int, total: int) -> int:
    if current >= total:
        return total
    nxt = ((current // interval) + 1) * interval
    if nxt > total:
        return total
    return nxt


def _tick_chunk(
    pyboy: Any,
    count: int,
    *,
    render_last: bool,
    render_first: bool = False,
) -> None:
    if count <= 0:
        return
    # A new button chord needs tick(1, render=True) before any render=False
    # batch so LCD/PPU run (Gen 1 warps/collision). PyBoy only draws the last
    # frame of tick(n, render=True); capture / until-eval still render last.
    if render_first:
        _tick_or_die(pyboy, 1, True)
        count -= 1
        if count == 0:
            return
    if render_last:
        if count > 1:
            _tick_without_render(pyboy, count - 1)
        _tick_or_die(pyboy, 1, True)
    else:
        _tick_without_render(pyboy, count)


def _tick_without_render(pyboy: Any, count: int) -> None:
    remaining = count
    while remaining > 0:
        n = min(_MAX_TICK_WITHOUT_RENDER, remaining)
        _tick_or_die(pyboy, n, False)
        remaining -= n


def _tick_or_die(pyboy: Any, count: int, render: bool) -> None:
    still = pyboy.tick(count, render)
    if still is False:
        raise RuntimeError("PyBoy session stopped while applying input")


def _set_buttons(pyboy: Any, pressed: set[str], desired: tuple[str, ...]) -> bool:
    want = set(desired)
    if want == pressed:
        return False
    # Different chord: drop everything, then press the new set.
    _release_all(pyboy, pressed)
    for name in desired:
        if name in pressed:
            continue
        pyboy.button_press(name)
        pressed.add(name)
    return True


def _release_all(pyboy: Any, pressed: set[str]) -> None:
    names = list(pressed)
    pressed.clear()
    release = getattr(pyboy, "button_release", None)
    for name in names:
        if release is None:
            continue
        try:
            release(name)
        except Exception:
            continue


def _maybe_report_step(steps_out: list[dict[str, Any]], phase: _Phase) -> None:
    if not phase.report:
        return
    steps_out.append(
        {
            "buttons": list(phase.buttons),
            "hold_frames": phase.frames,
            "step_index": phase.step_index,
        }
    )
