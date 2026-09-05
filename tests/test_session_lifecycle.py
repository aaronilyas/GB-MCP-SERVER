from __future__ import annotations

import time
from pathlib import Path

import pytest

import db
from gb_mcp import config
from gb_mcp.emulator.backend import FakeInstanceBackend
from gb_mcp.emulator.session import SessionManager
from gb_mcp.storage.roms import _state_path_for_rom

from conftest import FakePyBoy
from rom_builder import make_rom


def _write_mapped_rom(
    roms_dir: Path, *, email: str = "owner@example.com", name: str | None = None
) -> tuple[str, Path]:
    name = name or ("a" * db.SUBDIRECTORY_NAME_LENGTH)
    dest = roms_dir / name
    dest.mkdir()
    rom_path = dest / "tetris.gb"
    rom_path.write_bytes(make_rom(title=b"TETRIS"))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, email)
    return name, rom_path


def test_ping_does_not_tick_and_resets_idle(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy
    ticks_before = pyboy.ticks
    buttons_before = list(pyboy.buttons)
    remaining0 = session.seconds_until_idle_close()
    time.sleep(0.05)
    remaining_before = session.seconds_until_idle_close()
    assert remaining_before < remaining0

    result = pyboy_manager.ping("owner@example.com", name)
    assert result["alive"] is True
    assert result["idle_timeout_seconds"] == 30
    assert result["seconds_since_last_input"] < 0.05
    assert pyboy.ticks == ticks_before
    assert pyboy.buttons == buttons_before
    remaining_after = session.seconds_until_idle_close()
    assert remaining_after > remaining_before


def test_save_battery_writes_state_and_keeps_session_running(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    state = _state_path_for_rom(rom_path)
    assert not state.exists() or state.stat().st_size == 0

    result = pyboy_manager.save_battery("owner@example.com", name)
    assert result["saved"] is True
    assert state.is_file() and state.stat().st_size > 0

    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    assert session.is_running is True
    sent = pyboy_manager.send_input("owner@example.com", name, ["a"])
    assert sent["sent"] is True


def test_default_idle_timeout_is_2700(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "IDLE_TIMEOUT_SECONDS", 2700)
    manager = SessionManager(
        backend=FakeInstanceBackend(FakePyBoy),
        idle_timeout_seconds=None,
    )
    try:
        assert manager._idle_timeout() == 2700
    finally:
        manager.shutdown()


def test_idle_loop_does_not_tick_while_waiting(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy
    assert pyboy.ticks == 0
    time.sleep(0.1)
    assert pyboy.ticks == 0
    result = pyboy_manager.ping("owner@example.com", name)
    assert result["alive"] is True
    assert pyboy.ticks == 0


def test_load_emulation_speed_applied(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path, emulation_speed=4)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    assert session._pyboy.speed == 4


def test_stop_still_saves(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    stopped = pyboy_manager.stop("owner@example.com", name)
    assert stopped["stopped"] is True
    assert stopped["saved"] is True
    assert stopped["close_reason"] == "requested"
    assert stopped["running"] is False
    assert _state_path_for_rom(rom_path).read_bytes() == b"FAKESTATE"


def test_idle_timeout_still_saves_and_closes(isolated_db, roms_dir: Path) -> None:
    manager = SessionManager(pyboy_factory=FakePyBoy, idle_timeout_seconds=0.15)
    try:
        name, rom_path = _write_mapped_rom(roms_dir)
        result = manager.load("owner@example.com", name, rom_path)
        assert result["running"] is True
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            session = manager.get("owner@example.com")
            if session is not None and not session.is_running:
                break
            time.sleep(0.02)
        session = manager.get("owner@example.com")
        assert session is not None
        assert session.is_running is False
        assert session.close_reason == "idle_timeout"
        assert session.saved is True
        assert _state_path_for_rom(rom_path).read_bytes() == b"FAKESTATE"

        sent = manager.send_input("owner@example.com", name, ["a"])
        assert sent["sent"] is False
        assert "idle timeout" in sent["error"]
    finally:
        manager.shutdown()
