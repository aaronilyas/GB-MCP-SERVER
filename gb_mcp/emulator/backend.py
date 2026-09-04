"""Play-instance backends used by the MCP SessionManager.

Production talks to `gb-pyboy-instance` containers (see `instance.py`).
Unit tests inject `InProcessBackend` / `FakeInstanceBackend` so they never
touch Docker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from gb_mcp.emulator.loop import (
    INPUT_COMMAND_TIMEOUT_SECONDS,
    EmulatorSession,
    PyBoyFactory,
    _default_pyboy_factory,
    _state_path_for_rom,
)


def play_container_name(subdirectory: str) -> str:
    """Docker name for the play instance of a 32-hex subdirectory."""
    return f"gb-play-{subdirectory}"


class InstanceDeadError(RuntimeError):
    """The play instance exited or cannot be reached. Message is tool-safe."""


class InstanceHandle:
    """Backend-specific handle plus the host-side ROM identity."""

    def __init__(
        self,
        *,
        email: str,
        subdirectory: str,
        rom_path: Path,
        container_name: str,
        session: EmulatorSession | None = None,
        reused: bool = False,
    ) -> None:
        self.email = email
        self.subdirectory = subdirectory
        self.rom_path = rom_path
        self.container_name = container_name
        self.session = session
        self.reused = reused
        self.state_path = _state_path_for_rom(rom_path)


class InstanceBackend(Protocol):
    def start(
        self,
        email: str,
        subdirectory: str,
        rom_path: Path,
        *,
        idle_timeout_seconds: float,
    ) -> InstanceHandle: ...

    def is_running(self, handle: InstanceHandle) -> bool: ...

    def status(self, handle: InstanceHandle) -> dict[str, Any]: ...

    def send_input(
        self,
        handle: InstanceHandle,
        steps: list[dict[str, Any]],
        screenshot_mode: str,
        *,
        timeout: float = INPUT_COMMAND_TIMEOUT_SECONDS,
    ) -> dict[str, Any]: ...

    def request_stop(self, handle: InstanceHandle, reason: str) -> None: ...

    def join(self, handle: InstanceHandle, timeout: float | None = 15) -> None: ...

    def reap(self, handle: InstanceHandle) -> None: ...

    def close_reason(self, handle: InstanceHandle) -> str | None: ...


class InProcessBackend:
    """Run PyBoy in this process.

    Used inside `gb-pyboy-instance` (via `docker/instance_server.py`) and as
    the fake instance backend for unit tests.
    """

    def __init__(self, pyboy_factory: PyBoyFactory | None = None) -> None:
        self._pyboy_factory = pyboy_factory or _default_pyboy_factory

    def start(
        self,
        email: str,
        subdirectory: str,
        rom_path: Path,
        *,
        idle_timeout_seconds: float,
    ) -> InstanceHandle:
        session = EmulatorSession(
            email,
            subdirectory,
            rom_path,
            pyboy_factory=self._pyboy_factory,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        session.start()
        session.wait_ready()
        return InstanceHandle(
            email=email,
            subdirectory=subdirectory,
            rom_path=rom_path,
            container_name=play_container_name(subdirectory),
            session=session,
            reused=False,
        )

    def is_running(self, handle: InstanceHandle) -> bool:
        session = handle.session
        return session is not None and session.is_running

    def status(self, handle: InstanceHandle) -> dict[str, Any]:
        if handle.session is None:
            raise InstanceDeadError("Play instance is no longer running")
        return handle.session.status()

    def send_input(
        self,
        handle: InstanceHandle,
        steps: list[dict[str, Any]],
        screenshot_mode: str,
        *,
        timeout: float = INPUT_COMMAND_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        session = handle.session
        if session is None or not session.is_running:
            reason = None if session is None else session.close_reason
            raise InstanceDeadError(_dead_message(reason))
        return session.submit(
            "input",
            timeout=timeout,
            steps=steps,
            screenshot_mode=screenshot_mode,
        )

    def request_stop(self, handle: InstanceHandle, reason: str) -> None:
        if handle.session is not None:
            handle.session.request_stop(reason)

    def join(self, handle: InstanceHandle, timeout: float | None = 15) -> None:
        if handle.session is not None:
            handle.session.join(timeout)

    def reap(self, handle: InstanceHandle) -> None:
        return

    def close_reason(self, handle: InstanceHandle) -> str | None:
        if handle.session is None:
            return None
        return handle.session.close_reason


class FakeInstanceBackend(InProcessBackend):
    """Named alias for tests: in-process stand-in for `gb-pyboy-instance`."""


def _dead_message(reason: str | None) -> str:
    if reason == "idle_timeout":
        return (
            "PyBoy session closed after idle timeout; call load_subdirectory_rom "
            "to start it again"
        )
    return "Play instance is no longer running"
