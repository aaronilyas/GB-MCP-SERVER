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
    rewrite_host_email,
)
from gb_mcp.emulator.play_limits import BUTTONS
from gb_mcp.emulator.session import PlaySession, SessionManager
from gb_mcp.storage.roms import _state_path_for_rom

from conftest import FakePyBoy
from rom_builder import make_rom


def _write_mapped_rom(
    roms_dir: Path,
    *,
    email: str = "owner@example.com",
    name: str | None = None,
    title: bytes = b"TETRIS",
) -> tuple[str, Path]:
    name = name or ("a" * db.SUBDIRECTORY_NAME_LENGTH)
    dest = roms_dir / name
    dest.mkdir()
    rom_path = dest / "tetris.gb"
    rom_path.write_bytes(make_rom(title=title))
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
    assert result["email"] == "owner@example.com"
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
    assert pyboy.tick_calls == [1] * POST_RESTORE_SETTLE_FRAMES
    assert pyboy.tick_renders == [True] * POST_RESTORE_SETTLE_FRAMES
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
    save_overlaid = overlay_status(
        {"email": "player@example.com", "running": True},
        {"email": "instance", "saved": True},
    )
    assert save_overlaid["email"] == "player@example.com"
    assert save_overlaid["saved"] is True
    assert save_overlaid["running"] is True


def test_reset_stops_unlinks_snapshot_and_cold_boots(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    _state_path_for_rom(rom_path).write_bytes(b"POISON")
    _ram_path_for_rom(rom_path).write_bytes(b"OLDRAM")
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    pyboy_manager._backend._pyboy_factory = factory
    loaded = pyboy_manager.load("owner@example.com", name, rom_path)
    assert loaded["restored_state"] is True
    first = created[0]
    assert first.loaded_state == b"POISON"

    result = pyboy_manager.reset("owner@example.com", name, rom_path)
    assert result["started"] is True
    assert result["running"] is True
    assert result["already_running"] is False
    assert result["restored_state"] is False
    assert result["discarded"] is True
    assert result["email"] == "owner@example.com"
    assert "error" not in result
    assert first.stopped is True
    assert len(created) == 2
    assert created[1] is not first
    assert created[1].loaded_state is None
    assert created[1].ticks == 0
    assert not _state_path_for_rom(rom_path).exists()
    assert _ram_path_for_rom(rom_path).is_file()

    sent = pyboy_manager.send_input("owner@example.com", name, ["a"])
    assert sent["sent"] is True
    assert sent["email"] == "owner@example.com"


def test_reset_without_running_session_unlinks_poison_and_leaves_sram(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    _state_path_for_rom(rom_path).write_bytes(b"POISON")
    _ram_path_for_rom(rom_path).write_bytes(b"OLDRAM")
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    pyboy_manager._backend._pyboy_factory = factory
    result = pyboy_manager.reset("owner@example.com", name, rom_path)
    assert result["started"] is True
    assert result["running"] is True
    assert result["restored_state"] is False
    assert result["discarded"] is True
    assert created[0].loaded_state is None
    assert created[0].ticks == 0
    assert not _state_path_for_rom(rom_path).exists()
    assert _ram_path_for_rom(rom_path).read_bytes() == b"OLDRAM"


def test_reset_discard_false_keeps_snapshot_and_skips_restore(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    _state_path_for_rom(rom_path).write_bytes(b"FAKESTATE")
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    pyboy_manager._backend._pyboy_factory = factory
    pyboy_manager.load("owner@example.com", name, rom_path)
    result = pyboy_manager.reset(
        "owner@example.com",
        name,
        rom_path,
        discard_state=False,
        restore_state=False,
    )
    assert result["started"] is True
    assert result["restored_state"] is False
    assert result["discarded"] is False
    assert created[-1].loaded_state is None
    assert created[-1].ticks == 0
    assert _state_path_for_rom(rom_path).read_bytes() == b"FAKESTATE"


def test_reset_discard_true_forces_restore_false(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    _state_path_for_rom(rom_path).write_bytes(b"POISON")
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    pyboy_manager._backend._pyboy_factory = factory
    result = pyboy_manager.reset(
        "owner@example.com",
        name,
        rom_path,
        discard_state=True,
        restore_state=True,
    )
    assert result["restored_state"] is False
    assert result["discarded"] is True
    assert created[0].loaded_state is None
    assert not _state_path_for_rom(rom_path).exists()


class LeakySaveBackend(FakeInstanceBackend):
    """Simulate a Docker instance that stamps email='instance' on RPC replies."""

    def save(self, handle, *, timeout=10):
        result = dict(super().save(handle, timeout=timeout))
        result["email"] = "instance"
        return result

    def ping(self, handle, *, timeout=10):
        result = dict(super().ping(handle, timeout=timeout))
        result["email"] = "instance"
        return result

    def send_input(self, handle, steps, screenshot_mode, *, timeout=10, **extra):
        result = dict(
            super().send_input(
                handle, steps, screenshot_mode, timeout=timeout, **extra
            )
        )
        result["email"] = "instance"
        return result

    def status(self, handle):
        payload = dict(super().status(handle))
        payload["email"] = "instance"
        return payload


def test_rewrite_host_email_on_save_shaped_remote() -> None:
    rewritten = rewrite_host_email(
        {"email": "instance", "saved": True},
        "player@example.com",
    )
    assert rewritten["email"] == "player@example.com"
    assert rewritten["saved"] is True
    input_shaped = rewrite_host_email(
        {
            "email": "instance",
            "frames_advanced": 8,
            "pngs": [b"png"],
            "classifiers": {"overworld": True},
        },
        "player@example.com",
    )
    assert input_shaped["email"] == "player@example.com"
    assert input_shaped["frames_advanced"] == 8
    assert input_shaped["pngs"] == [b"png"]
    assert input_shaped["classifiers"] == {"overworld": True}
    assert "email" not in rewrite_host_email({"saved": True}, "player@example.com")
    assert rewrite_host_email({"email": "instance"}, None)["email"] == "instance"


def test_save_battery_rewrites_instance_placeholder_email(
    isolated_db, roms_dir: Path
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir, email="player@example.com")
    manager = SessionManager(
        backend=LeakySaveBackend(FakePyBoy),
        idle_timeout_seconds=30,
    )
    try:
        loaded = manager.load("player@example.com", name, rom_path)
        assert loaded["email"] == "player@example.com"
        result = manager.save_battery("player@example.com", name)
        assert result["saved"] is True
        assert result["email"] == "player@example.com"
        pinged = manager.ping("player@example.com", name)
        assert pinged["alive"] is True
        assert pinged["email"] == "player@example.com"
        sent = manager.send_input("player@example.com", name, ["a"])
        assert sent["sent"] is True
        assert sent["email"] == "player@example.com"
    finally:
        manager.shutdown()


def test_docker_rpc_rewrites_instance_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gb_mcp.emulator.backend import InstanceHandle
    from gb_mcp.emulator.instance import DockerInstanceBackend

    subdirectory = "a" * 32
    handle = InstanceHandle(
        email="player@example.com",
        subdirectory=subdirectory,
        rom_path=tmp_path / "game.gb",
        container_name="gb-play-" + subdirectory,
    )
    backend = DockerInstanceBackend()
    monkeypatch.setattr(
        "gb_mcp.emulator.instance._container_running", lambda _name: True
    )

    def fake_rpc(name, method, path, body, *, timeout):  # noqa: ARG001
        if path == "/input":
            return {
                "email": "instance",
                "subdirectory": "wrong",
                "frames_advanced": 4,
                "classifiers": {"overworld": True},
                "pngs_b64": [],
            }
        if path == "/ping":
            return {"email": "instance", "alive": True}
        if path == "/save":
            return {"email": "instance", "saved": True}
        if path == "/discard_state":
            return {"email": "instance", "discarded": True, "restored_state": False}
        if path == "/status":
            return {
                "email": "instance",
                "running": True,
                "restored_state": True,
                "saved": False,
            }
        raise AssertionError(path)

    monkeypatch.setattr("gb_mcp.emulator.instance._rpc", fake_rpc)

    saved = backend.save(handle)
    assert saved["email"] == "player@example.com"
    assert saved["saved"] is True

    pinged = backend.ping(handle)
    assert pinged["email"] == "player@example.com"
    assert pinged["alive"] is True

    sent = backend.send_input(handle, [{"buttons": ["a"]}], "final")
    assert sent["email"] == "player@example.com"
    assert sent["subdirectory"] == subdirectory
    assert sent["frames_advanced"] == 4
    assert sent["classifiers"] == {"overworld": True}
    assert sent["pngs"] == []

    discarded = backend.discard_state(handle)
    assert discarded["email"] == "player@example.com"
    assert discarded["discarded"] is True

    status = backend.status(handle)
    assert status["email"] == "player@example.com"
    assert status["restored_state"] is True
    assert status["running"] is True


def test_resolve_game_unique_title_case_insensitive(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    result = pyboy_manager.resolve_game("owner@example.com", title="tetris")
    assert "error" not in result
    assert result["title"] == "TETRIS"
    assert result["id"] == name
    assert result["subdirectory"] == name
    assert result["playable"] is True
    assert result["rom_path"] == rom_path
    again = pyboy_manager.resolve_game("owner@example.com", title="TeTrIs")
    assert again["id"] == name
    assert again["subdirectory"] == name


def test_resolve_game_duplicate_title_lists_matches(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    first, _ = _write_mapped_rom(roms_dir, name="a" * db.SUBDIRECTORY_NAME_LENGTH)
    second, _ = _write_mapped_rom(roms_dir, name="b" * db.SUBDIRECTORY_NAME_LENGTH)
    result = pyboy_manager.resolve_game("owner@example.com", title="TETRIS")
    assert "error" in result
    matches = result["matches"]
    assert {item["id"] for item in matches} == {first, second}
    for item in matches:
        assert item["title"] == "TETRIS"
        assert item["playable"] is True
        assert set(item) >= {"title", "id"}


def test_resolve_game_hex_id_disambiguates(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    first, first_rom = _write_mapped_rom(
        roms_dir, name="a" * db.SUBDIRECTORY_NAME_LENGTH
    )
    second, second_rom = _write_mapped_rom(
        roms_dir, name="b" * db.SUBDIRECTORY_NAME_LENGTH
    )
    by_id = pyboy_manager.resolve_game("owner@example.com", id=second)
    assert "error" not in by_id
    assert by_id["id"] == second
    assert by_id["subdirectory"] == second
    assert by_id["rom_path"] == second_rom
    disambiguated = pyboy_manager.resolve_game(
        "owner@example.com", title="tetris", id=second
    )
    assert "error" not in disambiguated
    assert disambiguated["id"] == second
    assert disambiguated["subdirectory"] == second
    assert disambiguated["rom_path"] == second_rom
    other = pyboy_manager.resolve_game("owner@example.com", title="TETRIS", id=first)
    assert other["id"] == first
    assert other["rom_path"] == first_rom


def test_resolve_game_unknown_title_id_and_other_owner(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    owned, _ = _write_mapped_rom(roms_dir)
    foreign, _ = _write_mapped_rom(
        roms_dir,
        email="other@example.com",
        name="c" * db.SUBDIRECTORY_NAME_LENGTH,
        title=b"ZELDA",
    )
    unknown_id = "f" * db.SUBDIRECTORY_NAME_LENGTH
    missing_title = pyboy_manager.resolve_game("owner@example.com", title="POKEMON")
    assert "error" in missing_title
    assert "matches" not in missing_title
    missing_id = pyboy_manager.resolve_game("owner@example.com", id=unknown_id)
    assert "error" in missing_id
    foreign_id = pyboy_manager.resolve_game("owner@example.com", id=foreign)
    assert "error" in foreign_id
    foreign_title = pyboy_manager.resolve_game("owner@example.com", title="ZELDA")
    assert "error" in foreign_title
    still_owned = pyboy_manager.resolve_game("owner@example.com", id=owned)
    assert still_owned["id"] == owned


def test_current_returns_running_session_or_idle_dict(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    idle = pyboy_manager.current("owner@example.com")
    assert idle["running"] is False
    assert idle["email"] == "owner@example.com"
    assert "error" in idle
    assert not isinstance(idle, PlaySession)

    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.current("owner@example.com")
    assert isinstance(session, PlaySession)
    assert session.is_running is True
    assert session is pyboy_manager.get("owner@example.com")
    assert session.subdirectory == name
