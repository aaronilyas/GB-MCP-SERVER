from __future__ import annotations

import time
from pathlib import Path

import pytest

import db
from gb_mcp import config
from gb_mcp.emulator.backend import FakeInstanceBackend
from gb_mcp.emulator.loop import (
    POST_RESTORE_SETTLE_FRAMES,
    _ram_path_for_rom,
    overlay_status,
)
from gb_mcp.emulator.play_limits import BUTTONS
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


def test_save_battery_writes_snapshot_and_keeps_session_running(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    """save_battery writes the PyBoy snapshot for resume; it is not SRAM.

    Stock PyBoy has no live battery dump; SRAM is flushed on stop(save=True).
    """
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy
    state = _state_path_for_rom(rom_path)
    ram = _ram_path_for_rom(rom_path)
    assert not state.exists() or state.stat().st_size == 0
    assert not ram.exists()

    result = pyboy_manager.save_battery("owner@example.com", name)
    assert result["saved"] is True
    assert state.is_file() and state.read_bytes() == b"FAKESTATE"
    assert not ram.exists()
    assert pyboy.stopped is False
    assert pyboy.saved_ram is False
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


def test_stop_writes_snapshot_and_sram(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy
    stopped = pyboy_manager.stop("owner@example.com", name)
    assert stopped["stopped"] is True
    assert stopped["saved"] is True
    assert stopped["close_reason"] == "requested"
    assert stopped["running"] is False
    assert _state_path_for_rom(rom_path).read_bytes() == b"FAKESTATE"
    assert pyboy.stopped is True
    assert pyboy.saved_ram is True
    assert _ram_path_for_rom(rom_path).read_bytes() == b"FAKERAM"


def test_idle_timeout_writes_snapshot_and_sram(isolated_db, roms_dir: Path) -> None:
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    manager = SessionManager(pyboy_factory=factory, idle_timeout_seconds=0.15)
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
        assert created[0].stopped is True
        assert created[0].saved_ram is True
        assert _ram_path_for_rom(rom_path).read_bytes() == b"FAKERAM"

        sent = manager.send_input("owner@example.com", name, ["a"])
        assert sent["sent"] is False
        assert "idle timeout" in sent["error"]
    finally:
        manager.shutdown()


def test_load_state_error_cold_boots_without_crash(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    _state_path_for_rom(rom_path).write_bytes(b"POISON")
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        instance.load_state_error = RuntimeError("bad snapshot")
        created.append(instance)
        return instance

    pyboy_manager._backend._pyboy_factory = factory
    result = pyboy_manager.load("owner@example.com", name, rom_path)
    assert result["running"] is True
    assert result["started"] is True
    assert result["restored_state"] is False
    assert "bad snapshot" in result.get("restore_error", "")
    assert created[0].ticks == 0
    assert created[0].loaded_state is None

    sent = pyboy_manager.send_input("owner@example.com", name, ["a"])
    assert sent["sent"] is True
    time.sleep(0.05)
    ticks_after_input = created[0].ticks
    time.sleep(0.05)
    assert created[0].ticks == ticks_after_input


def test_restore_settles_eight_frames_buttons_released(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    _state_path_for_rom(rom_path).write_bytes(b"FAKESTATE")
    created: list[FakePyBoy] = []

    class StickyFakePyBoy(FakePyBoy):
        def load_state(self, fh) -> None:
            super().load_state(fh)
            self.button_press("up")
            self.button_press("a")

    def factory(path: Path) -> FakePyBoy:
        instance = StickyFakePyBoy(path)
        created.append(instance)
        return instance

    pyboy_manager._backend._pyboy_factory = factory
    result = pyboy_manager.load("owner@example.com", name, rom_path)
    assert result["restored_state"] is True
    assert "restore_error" not in result
    pyboy = created[0]
    assert pyboy.loaded_state == b"FAKESTATE"
    assert pyboy.ticks == POST_RESTORE_SETTLE_FRAMES
    assert pyboy.tick_calls == [POST_RESTORE_SETTLE_FRAMES]
    assert pyboy.tick_renders == [True]
    assert BUTTONS.issubset(set(pyboy.releases))
    assert pyboy._pressed == set()

    time.sleep(0.1)
    assert pyboy.ticks == POST_RESTORE_SETTLE_FRAMES
    pinged = pyboy_manager.ping("owner@example.com", name)
    assert pinged["alive"] is True
    assert pyboy.ticks == POST_RESTORE_SETTLE_FRAMES


def test_save_battery_writes_sram_when_save_ram_exists(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    created: list[FakePyBoy] = []

    class RamDumpingPyBoy(FakePyBoy):
        def save_ram(self, fh) -> None:
            fh.write(b"FAKERAM")

    def factory(path: Path) -> FakePyBoy:
        instance = RamDumpingPyBoy(path)
        created.append(instance)
        return instance

    pyboy_manager._backend._pyboy_factory = factory
    pyboy_manager.load("owner@example.com", name, rom_path)
    result = pyboy_manager.save_battery("owner@example.com", name)
    assert result["saved"] is True
    assert _state_path_for_rom(rom_path).read_bytes() == b"FAKESTATE"
    assert _ram_path_for_rom(rom_path).read_bytes() == b"FAKERAM"
    assert created[0].stopped is False
    assert created[0].saved_ram is False


def test_load_restore_state_false_skips_existing_snapshot(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    _state_path_for_rom(rom_path).write_bytes(b"FAKESTATE")
    _ram_path_for_rom(rom_path).write_bytes(b"FAKERAM")
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    pyboy_manager._backend._pyboy_factory = factory
    result = pyboy_manager.load(
        "owner@example.com", name, rom_path, restore_state=False
    )
    assert result["running"] is True
    assert result["started"] is True
    assert result["restored_state"] is False
    assert "restore_error" not in result
    assert result["email"] == "owner@example.com"
    assert created[0].loaded_state is None
    assert created[0].ticks == 0
    assert _state_path_for_rom(rom_path).read_bytes() == b"FAKESTATE"
    assert _ram_path_for_rom(rom_path).read_bytes() == b"FAKERAM"

    time.sleep(0.05)
    assert created[0].ticks == 0
    pinged = pyboy_manager.ping("owner@example.com", name)
    assert pinged["alive"] is True
    assert pinged["email"] == "owner@example.com"
    assert created[0].ticks == 0


def test_load_restore_state_false_does_not_restart_running_session(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    first = pyboy_manager.load("owner@example.com", name, rom_path)
    assert first["already_running"] is False
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy
    second = pyboy_manager.load(
        "owner@example.com", name, rom_path, restore_state=False
    )
    assert second["already_running"] is True
    assert second["started"] is True
    assert pyboy_manager.get("owner@example.com") is session
    assert session._pyboy is pyboy


def test_discard_state_unlinks_snapshot_leaves_sram_and_pyboy(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    _state_path_for_rom(rom_path).write_bytes(b"FAKESTATE")
    _ram_path_for_rom(rom_path).write_bytes(b"FAKERAM")
    loaded = pyboy_manager.load("owner@example.com", name, rom_path)
    assert loaded["restored_state"] is True
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy
    ticks_before = pyboy.ticks
    buttons_before = list(pyboy.buttons)

    result = pyboy_manager.discard_state("owner@example.com", name)
    assert result["discarded"] is True
    assert result["restored_state"] is False
    assert result["email"] == "owner@example.com"
    assert result["running"] is True
    assert not _state_path_for_rom(rom_path).exists()
    assert _ram_path_for_rom(rom_path).read_bytes() == b"FAKERAM"
    assert pyboy.stopped is False
    assert pyboy.ticks == ticks_before
    assert pyboy.buttons == buttons_before
    assert session.is_running is True

    again = pyboy_manager.discard_state("owner@example.com", name)
    assert again["discarded"] is False
    assert again["restored_state"] is False
    assert not _state_path_for_rom(rom_path).exists()
    assert _ram_path_for_rom(rom_path).read_bytes() == b"FAKERAM"
    assert pyboy.stopped is False
    assert pyboy.ticks == ticks_before

    sent = pyboy_manager.send_input("owner@example.com", name, ["a"])
    assert sent["sent"] is True
    assert sent["email"] == "owner@example.com"


def test_status_keeps_host_email_over_instance_placeholder(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    loaded = pyboy_manager.load("owner@example.com", name, rom_path)
    assert loaded["email"] == "owner@example.com"
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    leaked = session.status(email="instance")
    assert leaked["email"] == "owner@example.com"
    overlaid = overlay_status(
        {"email": "owner@example.com", "running": True},
        {"email": "instance", "restored_state": True},
    )
    assert overlaid["email"] == "owner@example.com"
    assert overlaid["restored_state"] is True
    assert overlaid["running"] is True
