"""In-process PyBoy loop used inside `gb-pyboy-instance`.

The MCP host does not run this thread. It talks to a dedicated container
via `gb_mcp.emulator.instance`; this module is the guest implementation
and the fake backend used by unit tests.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gb_mcp import config
from gb_mcp.emulator.play_limits import (
    BUTTONS,
    DEFAULT_EMULATION_SPEED,
)

PyBoyFactory = Callable[[Path], Any]
REMOTE_STATUS_KEYS = (
    "restored_state",
    "restore_error",
    "idle_timeout_seconds",
    "seconds_until_idle_close",
    "seconds_since_last_input",
    "cartridge_title",
    "saved",
    "close_reason",
    "alive",
    "emulation_speed",
)

# Frames to run after a successful load_state so LCD/PPU leave a mid-frame
# snapshot. Poisoned restores present as stairs/doors not warping and the
# camera scrolling out of bounds.
POST_RESTORE_SETTLE_FRAMES = 8


def _state_path_for_rom(rom_path: Path) -> Path:
    """Return the PyBoy snapshot path stored next to the ROM (`rom.gb.state`).

    This is ``save_state`` / ``load_state`` output used to resume a session,
    not cartridge battery SRAM.
    """
    return Path(str(rom_path) + ".state")


def _ram_path_for_rom(rom_path: Path) -> Path:
    """Return the PyBoy cartridge SRAM path stored next to the ROM (`rom.gb.ram`).

    Stock PyBoy writes this from ``stop(save=True)``.
    """
    return Path(str(rom_path) + ".ram")


def shape_status(
    *,
    email: str,
    subdirectory: str,
    rom_path: Path,
    running: bool,
    saved: bool = False,
    close_reason: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Common play-status dict used by in-process and Docker backends."""
    try:
        display = str(rom_path.relative_to(config.ROOT))
    except ValueError:
        display = str(rom_path)
    payload: dict[str, Any] = {
        "email": email,
        "subdirectory": subdirectory,
        "rom": rom_path.name,
        "rom_path": display,
        "running": running,
        "saved": saved,
    }
    if close_reason:
        payload["close_reason"] = close_reason
    payload.update(fields)
    return payload


def overlay_status(base: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    """Copy selected fields from a remote instance status onto a host payload.

    The instance process hardcodes ``email="instance"``; keep the MCP host email.
    """
    host_email = base.get("email")
    for key in REMOTE_STATUS_KEYS:
        if key in remote:
            base[key] = remote[key]
    if host_email is not None:
        base["email"] = host_email
    return base


def _default_pyboy_factory(rom_path: Path) -> Any:
    from pyboy import PyBoy

    return PyBoy(
        str(rom_path),
        window=config.PYBOY_WINDOW,
        sound_emulated=False,
        no_input=True,
        log_level="ERROR",
    )


def _atomic_write_file(dest: Path, write: Callable[[Any], None], *, prefix: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as tmp:
            write(tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_state(pyboy: Any, dest: Path) -> None:
    _atomic_write_file(dest, pyboy.save_state, prefix=".state-")


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
        emulation_speed: int = DEFAULT_EMULATION_SPEED,
        restore_state: bool = True,
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
        self._restore_state = bool(restore_state)
        self._pyboy_factory = pyboy_factory
        self._idle_timeout = idle_timeout_seconds
        self._emulation_speed = (
            DEFAULT_EMULATION_SPEED if emulation_speed is None else int(emulation_speed)
        )
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
        payload = shape_status(
            email=self.email,
            subdirectory=self.subdirectory,
            rom_path=self.rom_path,
            running=self.is_running,
            saved=self.saved,
            close_reason=self.close_reason,
            restored_state=self.restored_state,
            idle_timeout_seconds=self._idle_timeout,
            seconds_until_idle_close=round(self.seconds_until_idle_close(), 3),
            seconds_since_last_input=round(self.seconds_since_last_input(), 3),
            emulation_speed=self._emulation_speed,
            cartridge_title=self.cartridge_title,
        )
        if self.restore_error:
            payload["restore_error"] = self.restore_error
        payload.update(extra)
        return payload

    def seconds_since_last_input(self) -> float:
        return max(0.0, time.monotonic() - self._last_input_at)

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
        if command.op == "ping":
            command.complete(self._apply_ping())
            return
        if command.op == "save":
            command.complete(self._apply_save())
            return
        if command.op == "discard_state":
            command.complete(self._apply_discard_state())
            return
        if command.op == "stop":
            self.close_reason = command.payload.get("reason", "requested")
            self._stop_requested.set()
            command.complete({"stopping": True})
            return
        raise ValueError(f"unknown PyBoy command {command.op!r}")

    def _apply_ping(self) -> dict[str, Any]:
        self._last_input_at = time.monotonic()
        return {
            "alive": True,
            "idle_timeout_seconds": self._idle_timeout,
            "seconds_since_last_input": 0.0,
        }

    def _apply_save(self) -> dict[str, Any]:
        """Persist the PyBoy snapshot at ``rom.gb.state`` without stopping.

        That file is ``save_state`` output for session resume, not cartridge
        battery. Stock PyBoy only writes SRAM in ``stop(save=True)``, which
        would end this session; idle and stop still snapshot then
        ``stop(save=True)``. If a live ``save_ram`` exists, SRAM is written
        to ``rom.gb.ram`` as well.
        """
        self._save_snapshot()
        self._try_save_cartridge_sram()
        return {"saved": True}

    def _apply_discard_state(self) -> dict[str, Any]:
        """Unlink ``rom.gb.state`` if present. Does not tick or stop PyBoy.

        Cartridge SRAM (``rom.gb.ram``) is left alone.
        """
        discarded = False
        try:
            self.state_path.unlink()
            discarded = True
        except FileNotFoundError:
            discarded = False
        except OSError:
            discarded = False
        self.restored_state = False
        self.saved = False
        return {"discarded": discarded, "restored_state": False}

    def _apply_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Lazy import: vision/numpy live in the play instance, not the MCP host image.
        from gb_mcp.emulator.play_runtime import execute_play_command, strip_forbidden_keys

        pyboy = self._pyboy
        if pyboy is None:
            raise RuntimeError("PyBoy instance is not running")
        body = dict(payload)
        nested = body.pop("play_payload", None)
        if isinstance(nested, dict):
            nested = dict(nested)
            nested.update({key: value for key, value in body.items() if key not in nested})
            body = nested
        if not body.get("steps"):
            body.pop("steps", None)
        try:
            result = execute_play_command(
                pyboy,
                body,
                session_speed=self._emulation_speed,
            )
        finally:
            if hasattr(pyboy, "set_emulation_speed"):
                try:
                    pyboy.set_emulation_speed(self._emulation_speed)
                except Exception:
                    pass
        self._last_input_at = time.monotonic()
        pngs = result.get("pngs")
        cleaned = strip_forbidden_keys(result)
        if pngs is not None:
            cleaned["pngs"] = pngs
        return cleaned

    def _save_snapshot(self) -> None:
        pyboy = self._pyboy
        if pyboy is None:
            return
        _atomic_write_state(pyboy, self.state_path)
        self.saved = True

    def _try_save_cartridge_sram(self) -> None:
        """Write cartridge SRAM without stopping, if PyBoy exposes a live dump."""
        pyboy = self._pyboy
        if pyboy is None:
            return
        save_ram = getattr(pyboy, "save_ram", None)
        if not callable(save_ram):
            return
        dest = _ram_path_for_rom(self.rom_path)
        try:
            _atomic_write_file(dest, save_ram, prefix=".ram-")
        except Exception:  # noqa: BLE001
            return

    def _release_all_buttons(self, pyboy: Any) -> None:
        release = getattr(pyboy, "button_release", None)
        if not callable(release):
            return
        for name in BUTTONS:
            try:
                release(name)
            except Exception:  # noqa: BLE001
                continue

    def _restore_snapshot(self, pyboy: Any) -> None:
        if not self._restore_state:
            return
        if not (self.state_path.is_file() and self.state_path.stat().st_size > 0):
            return
        try:
            with self.state_path.open("rb") as fh:
                pyboy.load_state(fh)
        except Exception as exc:  # noqa: BLE001
            self.restore_error = str(exc)
            self.restored_state = False
            return
        self._release_all_buttons(pyboy)
        pyboy.tick(POST_RESTORE_SETTLE_FRAMES, render=True)
        self.restored_state = True

    def _close_pyboy(self) -> None:
        pyboy = self._pyboy
        if pyboy is None:
            return
        try:
            # Cartridge SRAM (`rom.gb.ram`) is flushed here. The sibling
            # `.state` file is the PyBoy snapshot from `_save_snapshot`.
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
                pyboy.set_emulation_speed(self._emulation_speed)
            self._restore_snapshot(pyboy)
            self._last_input_at = time.monotonic()
            self._ready.set()

            while not self._stop_requested.is_set():
                commands = self._pop_commands()
                if commands:
                    for command in commands:
                        try:
                            self._handle(command)
                        except Exception as exc:  # noqa: BLE001
                            command.fail(exc)
                    continue
                if self._stop_requested.is_set():
                    break
                remaining = self._idle_timeout - (time.monotonic() - self._last_input_at)
                if remaining <= 0:
                    self.close_reason = "idle_timeout"
                    break
                self._cmd_available.wait(timeout=remaining)

            try:
                self._save_snapshot()
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
