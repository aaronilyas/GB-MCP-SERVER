#!/usr/bin/env python3
"""Local-only overworld warp repro. Not an MCP tool.

Boots PyBoy the same way as ``gb_mcp.emulator.loop._default_pyboy_factory``
(window=null, sound_emulated=False, no_input=True), walks one held d-pad
direction, writes LCD PNGs under ``/tmp/gb-warp-repro/``, and prints
``frames_advanced`` / ``restored_state``.

Modes:
  cold     — ignore sibling ``.state`` (PyBoy may still auto-load ``.ram``)
  restore  — load ``rom+.state`` like ``EmulatorSession._run``
  engine   — same restore as ``_run``; drive buttons only through
             ``run_play_input``

Walk ticks use ``input_engine._tick_chunk`` (engine mode goes through
``run_play_input``, which calls that helper). This script never writes
``.state`` / ``.sav`` / ``.ram`` and never registers an MCP tool.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gb_mcp.emulator.input_engine import (  # noqa: E402
    _next_eval_frame,
    _release_all,
    _set_buttons,
    _tick_chunk,
    run_play_input,
)
from gb_mcp.emulator.input_schema import parse_play_input  # noqa: E402
from gb_mcp.emulator.loop import (  # noqa: E402
    POST_RESTORE_SETTLE_FRAMES,
    _default_pyboy_factory,
    _state_path_for_rom,
)
from gb_mcp.emulator.play_limits import (  # noqa: E402
    BUTTONS,
    DEFAULT_EMULATION_SPEED,
    DEFAULT_HASH_REGIONS,
    DEFAULT_SCREENSHOT_SCALE,
    DEFAULT_UNTIL_EVAL_INTERVAL,
    MAX_CALL_TIMEOUT_SECONDS,
    MAX_FRAMES_PER_CALL,
    MAX_UNTIL_EVAL_INTERVAL,
    MIN_UNTIL_EVAL_INTERVAL,
    SCREENSHOT_SCALES,
)
from gb_mcp.emulator.vision import (  # noqa: E402
    ScreenshotPlan,
    capture_native,
    classify,
    encode_png,
    hash_named_regions,
    scale_nearest,
)

DIRECTIONS = ("up", "down", "left", "right")
MODES = ("cold", "restore", "engine")
DEFAULT_OUT_DIR = Path("/tmp/gb-warp-repro")
DEFAULT_FRAMES = 360
_KEYFRAME_FRACS = (0.25, 0.50, 0.75, 1.0)


def try_restore(pyboy: Any, state_path: Path) -> tuple[bool, str | None]:
    """Match ``EmulatorSession._run`` restore: nonempty ``rom.gb.state``."""
    try:
        if not (state_path.is_file() and state_path.stat().st_size > 0):
            return False, None
    except OSError as exc:
        return False, str(exc)
    try:
        with state_path.open("rb") as fh:
            pyboy.load_state(fh)
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def boot_pyboy(
    rom_path: Path,
    *,
    restore: bool,
    state_path: Path,
    emulation_speed: int,
    pyboy_factory: Any | None = None,
) -> tuple[Any, bool, str | None]:
    """Construct PyBoy via the production factory, then optionally load state."""
    factory = pyboy_factory or _default_pyboy_factory
    pyboy = factory(rom_path)
    try:
        if hasattr(pyboy, "set_emulation_speed"):
            pyboy.set_emulation_speed(emulation_speed)
        restored = False
        restore_error: str | None = None
        if restore:
            restored, restore_error = try_restore(pyboy, state_path)
            if restored:
                # Match EmulatorSession._restore_snapshot: PyBoy tick(n, True)
                # composes only the last frame of the batch.
                release = getattr(pyboy, "button_release", None)
                if callable(release):
                    for name in BUTTONS:
                        try:
                            release(name)
                        except Exception:  # noqa: BLE001
                            continue
                for _ in range(POST_RESTORE_SETTLE_FRAMES):
                    pyboy.tick(1, render=True)
        return pyboy, restored, restore_error
    except Exception:
        stop = getattr(pyboy, "stop", None)
        if stop is not None:
            try:
                stop(save=False)
            except Exception:
                pass
        raise


def save_lcd(pyboy: Any, dest: Path, *, scale: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    rgb = capture_native(pyboy)
    dest.write_bytes(encode_png(scale_nearest(rgb, scale)))
    return dest


def _keyframe_indexes(total: int, interval: int) -> set[int]:
    if total <= 0:
        return set()
    step = max(1, interval)
    indexes = {total}
    for frac in _KEYFRAME_FRACS:
        raw = max(1, int(round(total * frac)))
        snapped = ((raw + step - 1) // step) * step
        indexes.add(total if snapped > total else snapped)
    return indexes


def _lcd_debug(pyboy: Any) -> dict[str, Any]:
    """Framebuffer-only local debug. Never an MCP tool field."""
    try:
        frame = capture_native(pyboy)
    except Exception as exc:  # noqa: BLE001
        return {"lcd_error": str(exc)}
    try:
        hashes = hash_named_regions(frame, DEFAULT_HASH_REGIONS)
    except Exception as exc:  # noqa: BLE001
        hashes = {"error": str(exc)}
    try:
        flags = classify(frame)
    except Exception as exc:  # noqa: BLE001
        flags = {"error": str(exc)}
    return {"region_hashes": hashes, "classifiers": flags}


def walk_direct(
    pyboy: Any,
    *,
    direction: str,
    frames: int,
    interval: int,
    scale: int,
    prefix: Path,
    timeout: float,
) -> tuple[int, list[Path]]:
    """Hold ``direction`` for ``frames`` using the engine ``_tick_chunk`` policy.

    Same chunking as ``run_play_input`` for ``macro=hold`` with
    ``screenshot_mode=final`` and no until-monitor: split to the next
    ``until_eval_interval`` boundary (and the last frame), ``render_last``
    only on those boundaries.
    """
    pressed: set[str] = set()
    written: list[Path] = [save_lcd(pyboy, Path(str(prefix) + "_before.png"), scale=scale)]
    keyframes = _keyframe_indexes(frames, interval)
    frames_advanced = 0
    clock = time.monotonic
    start = clock()
    try:
        _set_buttons(pyboy, pressed, (direction,))
        while frames_advanced < frames:
            if clock() - start >= timeout:
                break
            phase_left = frames - frames_advanced
            next_eval = _next_eval_frame(frames_advanced, interval, frames)
            chunk = min(phase_left, next_eval - frames_advanced, frames - frames_advanced)
            if chunk <= 0:
                break
            end = frames_advanced + chunk
            is_last = end == frames
            need_eval = end == next_eval
            need_render = need_eval or is_last
            _tick_chunk(pyboy, chunk, render_last=need_render)
            frames_advanced = end
            if need_render and frames_advanced in keyframes and frames_advanced != frames:
                written.append(
                    save_lcd(
                        pyboy,
                        Path(f"{prefix}_{frames_advanced:04d}.png"),
                        scale=scale,
                    )
                )
            if clock() - start >= timeout:
                break
    finally:
        _release_all(pyboy, pressed)
    written.append(save_lcd(pyboy, Path(str(prefix) + "_after.png"), scale=scale))
    return frames_advanced, written


def walk_engine(
    pyboy: Any,
    *,
    direction: str,
    frames: int,
    interval: int,
    scale: int,
    prefix: Path,
    emulation_speed: int,
    timeout: float,
) -> tuple[int, list[Path], dict[str, Any]]:
    """Hold ``direction`` only through ``run_play_input`` (production scheduler)."""
    written: list[Path] = [save_lcd(pyboy, Path(str(prefix) + "_before.png"), scale=scale)]
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": [direction],
            "max_frames": frames,
            "hold_frames": frames,
            "emulation_speed": emulation_speed,
            "screenshot_mode": "final",
            "screenshot_scale": scale,
            "until_eval_interval": interval,
            "disable_default_hold_abort": True,
            "call_timeout_seconds": timeout,
        },
        session_speed=emulation_speed,
    )
    plan = ScreenshotPlan(play)
    result = run_play_input(
        pyboy,
        play,
        capture_native=lambda: capture_native(pyboy),
        until_monitor=None,
        screenshot_plan=plan,
    )
    pngs = result.get("pngs") or []
    for index, blob in enumerate(pngs):
        path = Path(f"{prefix}_engine_{index:02d}.png")
        path.write_bytes(blob)
        written.append(path)
    written.append(save_lcd(pyboy, Path(str(prefix) + "_after.png"), scale=scale))
    advanced = int(result.get("frames_advanced") or 0)
    return advanced, written, result


def run_repro(
    *,
    rom: Path,
    mode: str,
    direction: str,
    frames: int,
    out_dir: Path,
    state_path: Path | None = None,
    pyboy_factory: Any | None = None,
    emulation_speed: int = DEFAULT_EMULATION_SPEED,
    until_eval_interval: int = DEFAULT_UNTIL_EVAL_INTERVAL,
    screenshot_scale: int = DEFAULT_SCREENSHOT_SCALE,
    call_timeout_seconds: float = MAX_CALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Boot, walk, write PNGs. Returns local debug fields (not an MCP payload)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
    rom_path = Path(rom).expanduser().resolve()
    if not rom_path.is_file():
        raise FileNotFoundError(f"ROM not found: {rom_path}")
    state = Path(state_path).expanduser().resolve() if state_path else _state_path_for_rom(rom_path)
    restore = mode in {"restore", "engine"}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / f"{mode}_{direction}"
    interval = max(MIN_UNTIL_EVAL_INTERVAL, int(until_eval_interval))

    pyboy: Any = None
    try:
        pyboy, restored_state, restore_error = boot_pyboy(
            rom_path,
            restore=restore,
            state_path=state,
            emulation_speed=emulation_speed,
            pyboy_factory=pyboy_factory,
        )
        title = None
        try:
            title = pyboy.cartridge_title or None
        except Exception:  # noqa: BLE001
            title = None
        before_debug = _lcd_debug(pyboy)
        engine_result: dict[str, Any] | None = None
        if mode == "engine":
            frames_advanced, png_paths, engine_result = walk_engine(
                pyboy,
                direction=direction,
                frames=frames,
                interval=interval,
                scale=screenshot_scale,
                prefix=prefix,
                emulation_speed=emulation_speed,
                timeout=call_timeout_seconds,
            )
        else:
            frames_advanced, png_paths = walk_direct(
                pyboy,
                direction=direction,
                frames=frames,
                interval=interval,
                scale=screenshot_scale,
                prefix=prefix,
                timeout=call_timeout_seconds,
            )
        after_debug = _lcd_debug(pyboy)
    finally:
        if pyboy is not None:
            stop = getattr(pyboy, "stop", None)
            if stop is not None:
                try:
                    stop(save=False)
                except TypeError:
                    stop()
                except Exception:
                    pass

    payload: dict[str, Any] = {
        "frames_advanced": frames_advanced,
        "restored_state": bool(restored_state),
        "mode": mode,
        "direction": direction,
        "rom": str(rom_path),
        "state_path": str(state),
        "restore_error": restore_error,
        "cartridge_title": title,
        "png_dir": str(out_dir),
        "pngs": [str(path) for path in png_paths],
        "until_eval_interval": interval,
        "emulation_speed": emulation_speed,
        "before": before_debug,
        "after": after_debug,
    }
    if engine_result is not None:
        payload["stop_reason"] = engine_result.get("stop_reason")
        payload["until_fired"] = engine_result.get("until_fired")
        payload["macro"] = engine_result.get("macro")
    return payload


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Local overworld-warp repro: boot headless PyBoy, hold a d-pad "
            "direction, write LCD PNGs. Not an MCP tool."
        ),
        epilog=(
            "Examples:\n"
            "  .venv/bin/python scripts/repro_overworld_warp.py "
            "--rom /path/to/game.gb --mode restore --direction up --frames 360\n"
            "  .venv/bin/python scripts/repro_overworld_warp.py "
            "--rom /path/to/game.gb --mode engine --direction up --frames 360\n"
            "  .venv/bin/python scripts/repro_overworld_warp.py "
            "--rom /path/to/game.gb --mode cold --direction up --frames 360\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--rom", required=True, type=Path, help="Path to a .gb / .gbc dump (not committed)")
    parser.add_argument("--mode", required=True, choices=MODES, help="cold | restore | engine")
    parser.add_argument(
        "--direction",
        default="up",
        choices=DIRECTIONS,
        help="Held d-pad button (default: up, typical house door)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=DEFAULT_FRAMES,
        help=f"Held-walk length (default {DEFAULT_FRAMES}, max {MAX_FRAMES_PER_CALL})",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="PyBoy save-state path (default: <rom>.state, same as EmulatorSession)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"PNG directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--emulation-speed",
        type=int,
        default=DEFAULT_EMULATION_SPEED,
        help="pyboy.set_emulation_speed (default 0 = uncapped, same as play sessions)",
    )
    parser.add_argument(
        "--until-eval-interval",
        type=int,
        default=DEFAULT_UNTIL_EVAL_INTERVAL,
        help=(
            f"Tick-chunk / LCD-render interval (default {DEFAULT_UNTIL_EVAL_INTERVAL}, "
            "same as input_engine). Use 1 to render every frame."
        ),
    )
    parser.add_argument(
        "--screenshot-scale",
        type=int,
        default=DEFAULT_SCREENSHOT_SCALE,
        help="Nearest-neighbor upscale (default 4, same as send_pyboy_input)",
    )
    parser.add_argument(
        "--call-timeout-seconds",
        type=float,
        default=MAX_CALL_TIMEOUT_SECONDS,
        help=f"Wall-clock cap for the walk (default {MAX_CALL_TIMEOUT_SECONDS})",
    )
    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> None:
    if args.frames < 1 or args.frames > MAX_FRAMES_PER_CALL:
        raise SystemExit(f"--frames must be an integer from 1 to {MAX_FRAMES_PER_CALL}")
    if args.until_eval_interval < MIN_UNTIL_EVAL_INTERVAL or args.until_eval_interval > MAX_UNTIL_EVAL_INTERVAL:
        raise SystemExit(
            f"--until-eval-interval must be {MIN_UNTIL_EVAL_INTERVAL}..{MAX_UNTIL_EVAL_INTERVAL}"
        )
    if args.screenshot_scale not in SCREENSHOT_SCALES:
        raise SystemExit("--screenshot-scale must be 1, 2, 3, or 4")
    if args.call_timeout_seconds <= 0 or args.call_timeout_seconds > MAX_CALL_TIMEOUT_SECONDS:
        raise SystemExit(
            f"--call-timeout-seconds must be between 0 exclusive and {MAX_CALL_TIMEOUT_SECONDS}"
        )


def _print_result(payload: dict[str, Any]) -> None:
    # Required stdout contract.
    print(f"frames_advanced={payload['frames_advanced']}")
    print(f"restored_state={payload['restored_state']}")
    # Local debug only — do not copy these into MCP response schemas.
    print(f"mode={payload['mode']}")
    print(f"direction={payload['direction']}")
    print(f"rom={payload['rom']}")
    print(f"state_path={payload['state_path']}")
    if payload.get("restore_error"):
        print(f"restore_error={payload['restore_error']}")
    if payload.get("cartridge_title"):
        print(f"cartridge_title={payload['cartridge_title']}")
    if "stop_reason" in payload:
        print(f"stop_reason={payload['stop_reason']}")
    print(f"png_dir={payload['png_dir']}")
    for path in payload.get("pngs") or []:
        print(f"png={path}")
    before = payload.get("before") or {}
    after = payload.get("after") or {}
    if before.get("region_hashes"):
        print(f"region_hashes_before={before['region_hashes']}")
    if after.get("region_hashes"):
        print(f"region_hashes_after={after['region_hashes']}")
    if after.get("classifiers"):
        print(f"classifiers_after={after['classifiers']}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _validate_args(args)
    try:
        payload = run_repro(
            rom=args.rom,
            mode=args.mode,
            direction=args.direction,
            frames=args.frames,
            out_dir=args.out,
            state_path=args.state,
            emulation_speed=args.emulation_speed,
            until_eval_interval=args.until_eval_interval,
            screenshot_scale=args.screenshot_scale,
            call_timeout_seconds=args.call_timeout_seconds,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"repro failed: {exc}", file=sys.stderr)
        return 1
    _print_result(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
