"""In-process PyBoy loop used inside `gb-pyboy-instance`.

The MCP host does not run this thread. It talks to a dedicated container
via `gb_mcp.emulator.instance`; this module is the guest implementation
and the fake backend used by unit tests.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gb_mcp import config

BUTTONS = frozenset({"a", "b", "start", "select", "up", "down", "left", "right"})
MAX_HOLD_FRAMES = 120
MAX_INPUT_STEPS = 30
SCREENSHOT_MODES = frozenset({"final", "all"})
# button(name, N) is held for N ticks and released on tick N+1; worst batch is
# MAX_INPUT_STEPS * (MAX_HOLD_FRAMES + 1) frames at 60 fps, plus PNG encode.
INPUT_COMMAND_TIMEOUT_SECONDS = (
    MAX_INPUT_STEPS * (MAX_HOLD_FRAMES + 1)
) / 60.0 + 30.0
PyBoyFactory = Callable[[Path], Any]


def _state_path_for_rom(rom_path: Path) -> Path:
    """Return the PyBoy save-state path stored next to the ROM (`rom.gb.state`)."""
    return Path(str(rom_path) + ".state")


def _png_from_screen(pyboy: Any) -> bytes:
    """Encode the current PyBoy screen as PNG bytes. Must run on the emulator thread."""
    screen = getattr(pyboy, "screen", None)
    image = getattr(screen, "image", None) if screen is not None else None
    if image is None:
        raise RuntimeError("PyBoy screen image is unavailable")
    snapshot = image.copy() if hasattr(image, "copy") else image
    buf = io.BytesIO()
    snapshot.save(buf, format="PNG")
    data = buf.getvalue()
    if not data:
        raise RuntimeError("failed to encode PyBoy screenshot")
    return data


def _default_pyboy_factory(rom_path: Path) -> Any:
    from pyboy import PyBoy

    return PyBoy(
        str(rom_path),
        window=config.PYBOY_WINDOW,
        sound_emulated=False,
        no_input=True,
        log_level="ERROR",
    )


def _atomic_write_state(pyboy: Any, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as tmp:
            pyboy.save_state(tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class _Command:
    def __init__(self, op: str, **payload: Any) -> None:
        self.op = op
        self.payload = payload
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None

    def complete(self, result: Any = None) -> None:
        self.result = result
        self.event.set()

    def fail(self, error: BaseException) -> None:
        self.error = error
        self.event.set()

    def wait(self, timeout: float) -> Any:
        if not self.event.wait(timeout):
            raise TimeoutError(f"PyBoy command {self.op!r} timed out")
        if self.error is not None:
            raise self.error
        return self.result


class EmulatorSession:
    """One running PyBoy instance for a single email + subdirectory."""

    def __init__(
        self,
        email: str,
        subdirectory: str,
        rom_path: Path,
        *,
        pyboy_factory: PyBoyFactory,
        idle_timeout_seconds: float,
    ) -> None:
        self.email = email
        self.subdirectory = subdirectory
        self.rom_path = rom_path
        self.state_path = _state_path_for_rom(rom_path)
        self.cartridge_title: str | None = None
        self.restored_state = False
        self.restore_error: str | None = None
        self.close_reason: str | None = None
        self.saved = False
        self._pyboy_factory = pyboy_factory
        self._idle_timeout = idle_timeout_seconds
        self._last_input_at = time.monotonic()
        self._commands: list[_Command] = []
        self._cmd_lock = threading.Lock()
        self._cmd_available = threading.Event()
        self._stop_requested = threading.Event()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._start_error: BaseException | None = None
        self._pyboy: Any = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"pyboy-{email}-{subdirectory[:8]}",
            daemon=True,
        )

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive() and not self._closed.is_set()

    def start(self) -> None:
        self._thread.start()

    def wait_ready(self, timeout: float = 30) -> None:
        if not self._ready.wait(timeout):
            raise TimeoutError("PyBoy session failed to start")
        if self._start_error is not None:
            raise self._start_error

    def join(self, timeout: float | None = 15) -> None:
        self._thread.join(timeout)

    def request_stop(self, reason: str = "requested") -> None:
        self.close_reason = reason
        self._stop_requested.set()
        self._cmd_available.set()

    def submit(self, op: str, timeout: float = 10, **payload: Any) -> Any:
        if not self.is_running:
            raise RuntimeError("PyBoy session is not running")
        command = _Command(op, **payload)
        with self._cmd_lock:
            self._commands.append(command)
        self._cmd_available.set()
        return command.wait(timeout)

    def seconds_until_idle_close(self) -> float:
        if not self.is_running:
            return 0.0
        remaining = self._idle_timeout - (time.monotonic() - self._last_input_at)
        return max(0.0, remaining)

    def status(self, **extra: Any) -> dict[str, Any]:
        try:
            rom_path = str(self.rom_path.relative_to(config.ROOT))
        except ValueError:
            rom_path = str(self.rom_path)
        payload: dict[str, Any] = {
            "email": self.email,
            "subdirectory": self.subdirectory,
            "rom": self.rom_path.name,
            "rom_path": rom_path,
            "running": self.is_running,
            "restored_state": self.restored_state,
            "idle_timeout_seconds": self._idle_timeout,
            "seconds_until_idle_close": round(self.seconds_until_idle_close(), 3),
            "cartridge_title": self.cartridge_title,
            "saved": self.saved,
        }
        if self.restore_error:
            payload["restore_error"] = self.restore_error
        if self.close_reason:
            payload["close_reason"] = self.close_reason
        payload.update(extra)
        return payload

    def _pop_commands(self) -> list[_Command]:
        with self._cmd_lock:
            commands = self._commands
            self._commands = []
        self._cmd_available.clear()
        return commands

    def _handle(self, command: _Command) -> None:
        if command.op == "input":
            command.complete(self._apply_input(command.payload))
            return
        if command.op == "stop":
            self.close_reason = command.payload.get("reason", "requested")
            self._stop_requested.set()
            command.complete({"stopping": True})
            return
        raise ValueError(f"unknown PyBoy command {command.op!r}")

    def _apply_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        steps: list[dict[str, Any]] = payload["steps"]
        screenshot_mode: str = payload["screenshot_mode"]
        pyboy = self._pyboy
        if pyboy is None:
            raise RuntimeError("PyBoy instance is not running")
        if not steps:
            raise ValueError("steps must not be empty")
        if screenshot_mode not in SCREENSHOT_MODES:
            raise ValueError("screenshot_mode must be 'final' or 'all'")

        pngs: list[bytes] = []
        ran: list[dict[str, Any]] = []
        last_index = len(steps) - 1
        for index, step in enumerate(steps):
            buttons: list[str] = step["buttons"]
            hold_frames: int = step["hold_frames"]
            for button in buttons:
                pyboy.button(button, hold_frames)
            # Pressed for hold_frames ticks; released on tick hold_frames + 1.
            still_running = pyboy.tick(hold_frames + 1, True)
            if still_running is False:
                raise RuntimeError("PyBoy session stopped while applying input")
            ran.append(
                {"buttons": buttons, "hold_frames": hold_frames, "step_index": index}
            )
            if screenshot_mode == "all" or index == last_index:
                pngs.append(_png_from_screen(pyboy))

        self._last_input_at = time.monotonic()
        if screenshot_mode == "all":
            labels = [{"step_index": step["step_index"]} for step in ran]
        else:
            labels = [{"step_index": last_index}]
        return {
            "steps": ran,
            "screenshot_mode": screenshot_mode,
            "screenshot_count": len(pngs),
            "screenshots": labels,
            "pngs": pngs,
        }

    def _save(self) -> None:
        pyboy = self._pyboy
        if pyboy is None:
            return
        _atomic_write_state(pyboy, self.state_path)
        self.saved = True

    def _close_pyboy(self) -> None:
        pyboy = self._pyboy
        if pyboy is None:
            return
        try:
            pyboy.stop(save=True)
        finally:
            self._pyboy = None

    def _run(self) -> None:
        try:
            pyboy = self._pyboy_factory(self.rom_path)
            self._pyboy = pyboy
            try:
                title = pyboy.cartridge_title
            except Exception:
                title = None
            self.cartridge_title = title or None
            if hasattr(pyboy, "set_emulation_speed"):
                pyboy.set_emulation_speed(1)
            if self.state_path.is_file() and self.state_path.stat().st_size > 0:
                try:
                    with self.state_path.open("rb") as fh:
                        pyboy.load_state(fh)
                    self.restored_state = True
                except Exception as exc:  # noqa: BLE001
                    self.restore_error = str(exc)
            self._last_input_at = time.monotonic()
            self._ready.set()

            while not self._stop_requested.is_set():
                for command in self._pop_commands():
                    try:
                        self._handle(command)
                    except Exception as exc:  # noqa: BLE001
                        command.fail(exc)
                if self._stop_requested.is_set():
                    break
                if time.monotonic() - self._last_input_at >= self._idle_timeout:
                    self.close_reason = "idle_timeout"
                    break
                still_running = pyboy.tick()
                if still_running is False:
                    self.close_reason = "emulator_stopped"
                    break

            try:
                self._save()
            finally:
                self._close_pyboy()
        except Exception as exc:  # noqa: BLE001
            if not self._ready.is_set():
                self._start_error = exc
                self.close_reason = "error"
            elif self.close_reason is None:
                self.close_reason = "error"
            for command in self._pop_commands():
                command.fail(exc)
        finally:
            self._ready.set()
            self._closed.set()
            self._cmd_available.set()
