"""MCP-side play sessions: one live instance per user email.

Production starts/reuses a `gb-play-<subdir hex>` container. Unit tests
inject `FakeInstanceBackend` / `pyboy_factory` so they never talk to Docker.
"""

from __future__ import annotations

import atexit
import threading
from pathlib import Path
from typing import Any

from gb_mcp import config
from gb_mcp.emulator.backend import (
    InProcessBackend,
    InstanceBackend,
    InstanceDeadError,
    InstanceHandle,
)
from gb_mcp.emulator.loop import (
    BUTTONS,
    INPUT_COMMAND_TIMEOUT_SECONDS,
    MAX_HOLD_FRAMES,
    MAX_INPUT_STEPS,
    SCREENSHOT_MODES,
    EmulatorSession,
    PyBoyFactory,
)


class PlaySession:
    """MCP record of a play instance (container or in-process fake)."""

    def __init__(
        self,
        handle: InstanceHandle,
        backend: InstanceBackend,
        *,
        idle_timeout_seconds: float,
    ) -> None:
        self.email = handle.email
        self.subdirectory = handle.subdirectory
        self.rom_path = handle.rom_path
        self.state_path = handle.state_path
        self.container_name = handle.container_name
        self._handle = handle
        self._backend = backend
        self._idle_timeout = idle_timeout_seconds
        self.close_reason: str | None = None
        self._known_dead = False

    @property
    def is_running(self) -> bool:
        if self._known_dead:
            return False
        try:
            running = self._backend.is_running(self._handle)
        except Exception:
            running = False
        if not running:
            self._mark_dead()
        return running

    @property
    def saved(self) -> bool:
        session = self._handle.session
        if session is not None:
            return session.saved
        path = self.state_path
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @property
    def restored_state(self) -> bool:
        session = self._handle.session
        if session is not None:
            return session.restored_state
        try:
            return bool(self._backend.status(self._handle).get("restored_state"))
        except Exception:
            return False

    @property
    def restore_error(self) -> str | None:
        session = self._handle.session
        if session is not None:
            return session.restore_error
        return None

    @property
    def cartridge_title(self) -> str | None:
        session = self._handle.session
        if session is not None:
            return session.cartridge_title
        return None

    @property
    def _pyboy(self) -> Any:
        session = self._handle.session
        if session is None:
            return None
        return session._pyboy

    def seconds_until_idle_close(self) -> float:
        if not self.is_running:
            return 0.0
        try:
            remote = self._backend.status(self._handle)
        except Exception:
            return 0.0
        value = remote.get("seconds_until_idle_close")
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0

    def status(self, **extra: Any) -> dict[str, Any]:
        try:
            payload = self._backend.status(self._handle)
        except InstanceDeadError as exc:
            self._mark_dead()
            payload = {
                "email": self.email,
                "subdirectory": self.subdirectory,
                "rom": self.rom_path.name,
                "running": False,
                "error": str(exc),
            }
        except Exception:
            self._mark_dead()
            payload = {
                "email": self.email,
                "subdirectory": self.subdirectory,
                "rom": self.rom_path.name,
                "running": False,
                "error": "Play instance is no longer running",
            }
        payload.setdefault("email", self.email)
        payload.setdefault("subdirectory", self.subdirectory)
        payload.setdefault("idle_timeout_seconds", self._idle_timeout)
        if self.close_reason and "close_reason" not in payload:
            payload["close_reason"] = self.close_reason
        payload.update(extra)
        return payload

    def request_stop(self, reason: str = "requested") -> None:
        self.close_reason = reason
        try:
            self._backend.request_stop(self._handle, reason)
        except InstanceDeadError:
            self._mark_dead()

    def join(self, timeout: float | None = 15) -> None:
        try:
            self._backend.join(self._handle, timeout)
        finally:
            if not self._backend.is_running(self._handle):
                self._mark_dead(reap=False)

    def submit(self, op: str, timeout: float = 10, **payload: Any) -> Any:
        if op != "input":
            raise ValueError(f"unknown PyBoy command {op!r}")
        if not self.is_running:
            raise InstanceDeadError(_dead_message(self.close_reason))
        return self._backend.send_input(
            self._handle,
            payload["steps"],
            payload["screenshot_mode"],
            timeout=timeout,
        )

    def _mark_dead(self, *, reap: bool = True) -> None:
        if self._known_dead:
            return
        self._known_dead = True
        if self.close_reason is None:
            try:
                self.close_reason = self._backend.close_reason(self._handle)
            except Exception:
                self.close_reason = None
            if self.close_reason is None:
                self.close_reason = "instance_exited"
        if reap:
            try:
                self._backend.reap(self._handle)
            except Exception:
                pass


class SessionManager:
    """Process-wide registry: one running play instance per user email."""

    def __init__(
        self,
        *,
        backend: InstanceBackend | None = None,
        pyboy_factory: PyBoyFactory | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        self._pyboy_factory = pyboy_factory
        if backend is not None:
            self._backend: InstanceBackend = backend
        elif pyboy_factory is not None:
            self._backend = InProcessBackend(pyboy_factory)
        else:
            from gb_mcp.emulator.instance import DockerInstanceBackend

            self._backend = DockerInstanceBackend()
        self._idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.Lock()
        self._by_email: dict[str, PlaySession] = {}

    def _idle_timeout(self) -> float:
        if self._idle_timeout_seconds is not None:
            return float(self._idle_timeout_seconds)
        return float(config.IDLE_TIMEOUT_SECONDS)

    def get(self, email: str) -> PlaySession | None:
        with self._lock:
            return self._by_email.get(email)

    def load(self, email: str, subdirectory: str, rom_path: Path) -> dict[str, Any]:
        switched_from: str | None = None
        current: PlaySession | None = None
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
            try:
                handle = self._backend.start(
                    email,
                    subdirectory,
                    rom_path,
                    idle_timeout_seconds=self._idle_timeout(),
                )
            except InstanceDeadError as exc:
                return {
                    "started": False,
                    "running": False,
                    "email": email,
                    "subdirectory": subdirectory,
                    "error": str(exc),
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "started": False,
                    "running": False,
                    "email": email,
                    "subdirectory": subdirectory,
                    "error": f"failed to start PyBoy: {_tool_error(exc)}",
                }
            session = PlaySession(
                handle,
                self._backend,
                idle_timeout_seconds=self._idle_timeout(),
            )
            self._by_email[email] = session

        extra: dict[str, Any] = {
            "started": True,
            "already_running": bool(handle.reused),
        }
        if switched_from is not None:
            extra["switched_from"] = switched_from
            extra["previous_session_saved"] = current.saved if current is not None else False
        try:
            return session.status(**extra)
        except Exception as exc:  # noqa: BLE001
            return {
                "started": False,
                "running": False,
                "email": email,
                "subdirectory": subdirectory,
                "error": f"failed to start PyBoy: {_tool_error(exc)}",
            }

    def send_input(
        self,
        email: str,
        subdirectory: str,
        buttons: list[str] | None = None,
        hold_frames: int = 1,
        *,
        steps: list[dict[str, Any]] | None = None,
        screenshot_mode: str = "final",
    ) -> dict[str, Any]:
        session = self._require_running(email, subdirectory)
        if isinstance(session, dict):
            return session
        if steps:
            payload_steps = steps
        elif buttons:
            payload_steps = [{"buttons": buttons, "hold_frames": hold_frames}]
        else:
            return session.status(sent=False, error="at least one button is required")
        try:
            result = session.submit(
                "input",
                timeout=INPUT_COMMAND_TIMEOUT_SECONDS,
                steps=payload_steps,
                screenshot_mode=screenshot_mode,
            )
        except InstanceDeadError as exc:
            return session.status(sent=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return session.status(sent=False, error=_tool_error(exc))
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

    def _require_running(self, email: str, subdirectory: str) -> PlaySession | dict[str, Any]:
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
            elif reason in {"instance_exited", "error", "emulator_stopped"}:
                error = "Play instance is no longer running"
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


def _dead_message(reason: str | None) -> str:
    if reason == "idle_timeout":
        return (
            "PyBoy session closed after idle timeout; call load_subdirectory_rom "
            "to start it again"
        )
    return "Play instance is no longer running"


def _tool_error(exc: BaseException) -> str:
    """Surface a short error; never pass docker dumps through to tools."""
    text = str(exc).strip() or exc.__class__.__name__
    if "\n" in text or len(text) > 200:
        return "Play instance is no longer running"
    return text


manager = SessionManager()
atexit.register(manager.shutdown)
