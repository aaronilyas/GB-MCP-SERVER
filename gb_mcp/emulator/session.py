"""Long-lived PyBoy sessions with idle auto-save.

A session is keyed by the LLM user's email. The emulator thread ticks until
`stop` is requested or no button input arrives for `IDLE_TIMEOUT_SECONDS`
(default 5 minutes). Either path writes a PyBoy save state next to the ROM
and then closes the instance.
"""

from __future__ import annotations

import atexit
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gb_mcp import config
from gb_mcp.storage.roms import _state_path_for_rom

BUTTONS = frozenset({"a", "b", "start", "select", "up", "down", "left", "right"})
MAX_HOLD_FRAMES = 120
PyBoyFactory = Callable[[Path], Any]


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
            buttons: list[str] = command.payload["buttons"]
            hold_frames: int = command.payload["hold_frames"]
            pyboy = self._pyboy
            if pyboy is None:
                raise RuntimeError("PyBoy instance is not running")
            for button in buttons:
                pyboy.button(button, hold_frames)
            self._last_input_at = time.monotonic()
            command.complete({"buttons": buttons, "hold_frames": hold_frames})
            return
        if command.op == "stop":
            self.close_reason = command.payload.get("reason", "requested")
            self._stop_requested.set()
            command.complete({"stopping": True})
            return
        raise ValueError(f"unknown PyBoy command {command.op!r}")

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
            if self.state_path.is_file():
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


class SessionManager:
    """Process-wide registry: one running PyBoy session per user email."""

    def __init__(
        self,
        *,
        pyboy_factory: PyBoyFactory | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        self._pyboy_factory = pyboy_factory or _default_pyboy_factory
        self._idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.Lock()
        self._by_email: dict[str, EmulatorSession] = {}

    def _idle_timeout(self) -> float:
        if self._idle_timeout_seconds is not None:
            return float(self._idle_timeout_seconds)
        return float(config.IDLE_TIMEOUT_SECONDS)

    def get(self, email: str) -> EmulatorSession | None:
        with self._lock:
            return self._by_email.get(email)

    def load(self, email: str, subdirectory: str, rom_path: Path) -> dict[str, Any]:
        switched_from: str | None = None
        with self._lock:
            current = self._by_email.get(email)
            if current is not None and current.is_running:
                if current.subdirectory == subdirectory:
                    return current.status(started=True, already_running=True)
                switched_from = current.subdirectory
                current.request_stop(reason="switched")
                current.join(timeout=15)
                if current.is_running:
                    return {
                        "started": False,
                        "running": True,
                        "email": email,
                        "subdirectory": current.subdirectory,
                        "error": "failed to stop the existing PyBoy session before switching",
                    }
            session = EmulatorSession(
                email,
                subdirectory,
                rom_path,
                pyboy_factory=self._pyboy_factory,
                idle_timeout_seconds=self._idle_timeout(),
            )
            self._by_email[email] = session
            session.start()

        try:
            session.wait_ready()
        except Exception as exc:  # noqa: BLE001
            return {
                "started": False,
                "running": False,
                "email": email,
                "subdirectory": subdirectory,
                "error": f"failed to start PyBoy: {exc}",
            }

        extra: dict[str, Any] = {"started": True, "already_running": False}
        if switched_from is not None:
            extra["switched_from"] = switched_from
            extra["previous_session_saved"] = current.saved if current is not None else False
        return session.status(**extra)

    def send_input(
        self,
        email: str,
        subdirectory: str,
        buttons: list[str],
        hold_frames: int = 1,
    ) -> dict[str, Any]:
        session = self._require_running(email, subdirectory)
        if isinstance(session, dict):
            return session
        try:
            result = session.submit("input", buttons=buttons, hold_frames=hold_frames)
        except Exception as exc:  # noqa: BLE001
            return session.status(sent=False, error=str(exc))
        return session.status(sent=True, **result)

    def stop(self, email: str, subdirectory: str) -> dict[str, Any]:
        with self._lock:
            session = self._by_email.get(email)
        if session is None:
            return {
                "stopped": False,
                "email": email,
                "subdirectory": subdirectory,
                "error": "no PyBoy session is running for this email",
            }
        if session.subdirectory != subdirectory:
            return {
                "stopped": False,
                "email": email,
                "subdirectory": subdirectory,
                "running_subdirectory": session.subdirectory,
                "error": (
                    f"running PyBoy session is for subdirectory {session.subdirectory!r}, "
                    f"not {subdirectory!r}"
                ),
            }
        if not session.is_running:
            return session.status(
                stopped=True,
                already_stopped=True,
            )
        session.request_stop(reason="requested")
        session.join(timeout=15)
        if session.is_running:
            return session.status(stopped=False, error="PyBoy session did not stop in time")
        return session.status(stopped=True, already_stopped=False)

    def _require_running(self, email: str, subdirectory: str) -> EmulatorSession | dict[str, Any]:
        with self._lock:
            session = self._by_email.get(email)
        if session is None or not session.is_running:
            reason = None if session is None else session.close_reason
            error = "no PyBoy session is running for this email"
            if reason == "idle_timeout":
                error = (
                    "PyBoy session closed after idle timeout; call load_subdirectory_rom "
                    "to start it again"
                )
            return {
                "sent": False,
                "email": email,
                "subdirectory": subdirectory,
                "running": False,
                "error": error,
            }
        if session.subdirectory != subdirectory:
            return {
                "sent": False,
                "email": email,
                "subdirectory": subdirectory,
                "running_subdirectory": session.subdirectory,
                "running": True,
                "error": (
                    f"running PyBoy session is for subdirectory {session.subdirectory!r}, "
                    f"not {subdirectory!r}"
                ),
            }
        return session

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._by_email.values())
        for session in sessions:
            if session.is_running:
                session.request_stop(reason="shutdown")
                session.join(timeout=15)


manager = SessionManager()
atexit.register(manager.shutdown)
