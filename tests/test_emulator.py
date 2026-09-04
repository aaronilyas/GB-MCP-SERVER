from __future__ import annotations

import time
from pathlib import Path

import db
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


def test_load_starts_session_and_stop_saves(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    result = pyboy_manager.load("owner@example.com", name, rom_path)
    assert result["started"] is True
    assert result["running"] is True
    assert result["already_running"] is False
    assert result["rom"] == "tetris.gb"
    assert result["idle_timeout_seconds"] == 30

    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    assert session._pyboy.speed == 1

    sent = pyboy_manager.send_input("owner@example.com", name, ["a", "start"], hold_frames=4)
    assert sent["sent"] is True
    assert sent["buttons"] == ["a", "start"]
    assert session._pyboy.buttons == [("a", 4), ("start", 4)]

    stopped = pyboy_manager.stop("owner@example.com", name)
    assert stopped["stopped"] is True
    assert stopped["saved"] is True
    assert stopped["close_reason"] == "requested"
    assert stopped["running"] is False
    state = _state_path_for_rom(rom_path)
    assert state.read_bytes() == b"FAKESTATE"
    assert session._pyboy is None


def test_load_same_subdirectory_is_idempotent(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    first = pyboy_manager.load("owner@example.com", name, rom_path)
    second = pyboy_manager.load("owner@example.com", name, rom_path)
    assert first["already_running"] is False
    assert second["already_running"] is True
    assert second["started"] is True
    assert pyboy_manager.get("owner@example.com") is not None


def test_load_other_subdirectory_saves_and_switches(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    first_name, first_rom = _write_mapped_rom(roms_dir, name="b" * db.SUBDIRECTORY_NAME_LENGTH)
    second_name, second_rom = _write_mapped_rom(roms_dir, name="c" * db.SUBDIRECTORY_NAME_LENGTH)
    pyboy_manager.load("owner@example.com", first_name, first_rom)
    result = pyboy_manager.load("owner@example.com", second_name, second_rom)
    assert result["started"] is True
    assert result["subdirectory"] == second_name
    assert result["switched_from"] == first_name
    assert result["previous_session_saved"] is True
    assert _state_path_for_rom(first_rom).read_bytes() == b"FAKESTATE"


def test_idle_timeout_autosaves_and_closes(isolated_db, roms_dir: Path) -> None:
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


def test_input_resets_idle_timer(isolated_db, roms_dir: Path) -> None:
    manager = SessionManager(pyboy_factory=FakePyBoy, idle_timeout_seconds=0.25)
    try:
        name, rom_path = _write_mapped_rom(roms_dir)
        manager.load("owner@example.com", name, rom_path)
        time.sleep(0.12)
        sent = manager.send_input("owner@example.com", name, ["b"])
        assert sent["sent"] is True
        time.sleep(0.12)
        session = manager.get("owner@example.com")
        assert session is not None
        assert session.is_running is True
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not session.is_running:
                break
            time.sleep(0.02)
        assert session.is_running is False
        assert session.close_reason == "idle_timeout"
    finally:
        manager.shutdown()


def test_restores_save_state_on_reload(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    pyboy_manager.stop("owner@example.com", name)

    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    pyboy_manager._pyboy_factory = factory
    result = pyboy_manager.load("owner@example.com", name, rom_path)
    assert result["restored_state"] is True
    assert created[0].loaded_state == b"FAKESTATE"


def test_real_pyboy_load_input_stop_saves(isolated_db, roms_dir: Path) -> None:
    manager = SessionManager(idle_timeout_seconds=30)
    try:
        name = "1" * db.SUBDIRECTORY_NAME_LENGTH
        dest = roms_dir / name
        dest.mkdir()
        rom_path = dest / "game.gb"
        rom_path.write_bytes(make_rom(size=32 * 1024, title=b"TESTGAME"))
        result = manager.load("owner@example.com", name, rom_path)
        assert result["started"] is True
        assert result["running"] is True
        assert result["cartridge_title"] == "TESTGAME"
        sent = manager.send_input("owner@example.com", name, ["a"])
        assert sent["sent"] is True
        stopped = manager.stop("owner@example.com", name)
        assert stopped["stopped"] is True
        assert stopped["saved"] is True
        assert _state_path_for_rom(rom_path).stat().st_size > 0
    finally:
        manager.shutdown()


def test_stop_wrong_subdirectory(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    other = "d" * db.SUBDIRECTORY_NAME_LENGTH
    pyboy_manager.load("owner@example.com", name, rom_path)
    result = pyboy_manager.stop("owner@example.com", other)
    assert result["stopped"] is False
    assert "not" in result["error"]
