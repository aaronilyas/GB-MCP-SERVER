from __future__ import annotations

import io
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image as PILImage

import db
from gb_mcp.emulator.backend import FakeInstanceBackend, play_container_name
from gb_mcp.emulator.loop import _default_pyboy_factory
from gb_mcp.emulator.session import SessionManager
from gb_mcp.storage.roms import _state_path_for_rom

from conftest import FakePyBoy
from rom_builder import make_rom

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _assert_png(data: bytes) -> PILImage.Image:
    assert data.startswith(PNG_MAGIC)
    image = PILImage.open(io.BytesIO(data))
    image.load()
    return image


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
    assert session.container_name == play_container_name(name)
    assert session._pyboy.speed == 1

    sent = pyboy_manager.send_input("owner@example.com", name, ["a", "start"], hold_frames=4)
    assert sent["sent"] is True
    assert sent["steps"] == [{"buttons": ["a", "start"], "hold_frames": 4, "step_index": 0}]
    assert sent["screenshot_mode"] == "final"
    assert sent["screenshot_count"] == 1
    assert session._pyboy.buttons == [("a", 4), ("start", 4)]
    assert len(sent["pngs"]) == 1
    _assert_png(sent["pngs"][0])

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

    pyboy_manager._backend._pyboy_factory = factory
    result = pyboy_manager.load("owner@example.com", name, rom_path)
    assert result["restored_state"] is True
    assert created[0].loaded_state == b"FAKESTATE"


def test_real_pyboy_load_input_stop_saves(isolated_db, roms_dir: Path) -> None:
    manager = SessionManager(
        pyboy_factory=_default_pyboy_factory,
        idle_timeout_seconds=30,
    )
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
        assert sent["screenshot_count"] == 1
        png = sent["pngs"][0]
        image = _assert_png(png)
        assert image.size[0] >= 160
        assert image.size[1] >= 144
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


_THREE_STEPS = [
    {"buttons": ["a"], "hold_frames": 1},
    {"buttons": ["b", "right"], "hold_frames": 2},
    {"buttons": ["start"], "hold_frames": 3},
]


def test_send_input_single_step_returns_one_png(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    sent = pyboy_manager.send_input(
        "owner@example.com", name, ["a"], screenshot_mode="final"
    )
    assert sent["sent"] is True
    assert sent["screenshot_mode"] == "final"
    assert sent["screenshot_count"] == 1
    assert sent["screenshots"] == [{"step_index": 0}]
    assert len(sent["pngs"]) == 1
    _assert_png(sent["pngs"][0])


def test_send_input_steps_all_returns_one_png_per_step(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy

    sent = pyboy_manager.send_input(
        "owner@example.com",
        name,
        steps=_THREE_STEPS,
        screenshot_mode="all",
    )
    assert sent["sent"] is True
    assert sent["screenshot_count"] == 3
    assert sent["screenshots"] == [
        {"step_index": 0},
        {"step_index": 1},
        {"step_index": 2},
    ]
    assert len(sent["pngs"]) == 3
    images = [_assert_png(png) for png in sent["pngs"]]
    assert sent["pngs"][0] != sent["pngs"][1] != sent["pngs"][2]
    assert images[0].tobytes() != images[1].tobytes() != images[2].tobytes()
    assert pyboy.captures[-3:] == sorted(pyboy.captures[-3:])
    assert pyboy.captures[-3] < pyboy.captures[-2] < pyboy.captures[-1]


def test_send_input_steps_final_returns_last_png_only(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    sent = pyboy_manager.send_input(
        "owner@example.com",
        name,
        steps=_THREE_STEPS,
        screenshot_mode="final",
    )
    assert sent["sent"] is True
    assert sent["screenshot_count"] == 1
    assert sent["screenshots"] == [{"step_index": 2}]
    assert len(sent["pngs"]) == 1
    _assert_png(sent["pngs"][0])


def test_send_input_applies_steps_in_order(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    pyboy = session._pyboy

    sent = pyboy_manager.send_input(
        "owner@example.com",
        name,
        steps=_THREE_STEPS,
        screenshot_mode="all",
    )
    assert sent["sent"] is True
    assert pyboy.buttons == [("a", 1), ("b", 2), ("right", 2), ("start", 3)]
    assert [count for count in pyboy.tick_calls if count > 1] == [2, 3, 4]
    assert sent["steps"] == [
        {"buttons": ["a"], "hold_frames": 1, "step_index": 0},
        {"buttons": ["b", "right"], "hold_frames": 2, "step_index": 1},
        {"buttons": ["start"], "hold_frames": 3, "step_index": 2},
    ]


def test_batch_input_resets_idle_timer_once(isolated_db, roms_dir: Path) -> None:
    manager = SessionManager(pyboy_factory=FakePyBoy, idle_timeout_seconds=0.25)
    try:
        name, rom_path = _write_mapped_rom(roms_dir)
        manager.load("owner@example.com", name, rom_path)
        time.sleep(0.12)
        sent = manager.send_input(
            "owner@example.com",
            name,
            steps=_THREE_STEPS,
            screenshot_mode="final",
        )
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


def test_input_fails_without_partial_pngs_if_emulator_stops(
    isolated_db, roms_dir: Path
) -> None:
    class StoppingPyBoy(FakePyBoy):
        def tick(self, count: int = 1, render: bool = True, sound: bool = True) -> bool:
            if count > 1:
                self._input_batches = getattr(self, "_input_batches", 0) + 1
                if self._input_batches >= 2:
                    super().tick(count, render, sound)
                    self._dead.set()
                    return False
            return super().tick(count, render, sound)

    manager = SessionManager(
        backend=FakeInstanceBackend(StoppingPyBoy),
        idle_timeout_seconds=30,
    )
    try:
        name, rom_path = _write_mapped_rom(roms_dir)
        manager.load("owner@example.com", name, rom_path)
        sent = manager.send_input(
            "owner@example.com",
            name,
            steps=_THREE_STEPS,
            screenshot_mode="all",
        )
        assert sent["sent"] is False
        assert "stopped" in sent["error"]
        assert "pngs" not in sent
    finally:
        manager.shutdown()


def test_default_backend_is_docker() -> None:
    from gb_mcp.emulator.instance import DockerInstanceBackend

    manager = SessionManager()
    assert isinstance(manager._backend, DockerInstanceBackend)


def test_stop_leaves_restoreable_save_after_instance_removed(
    isolated_db, roms_dir: Path, pyboy_manager: SessionManager
) -> None:
    name, rom_path = _write_mapped_rom(roms_dir)
    pyboy_manager.load("owner@example.com", name, rom_path)
    session = pyboy_manager.get("owner@example.com")
    assert session is not None
    assert session.container_name == f"gb-play-{name}"
    stopped = pyboy_manager.stop("owner@example.com", name)
    assert stopped["stopped"] is True
    assert session.is_running is False
    state = _state_path_for_rom(rom_path)
    assert state.is_file()
    assert state.read_bytes() == b"FAKESTATE"

    result = pyboy_manager.load("owner@example.com", name, rom_path)
    assert result["started"] is True
    assert result["restored_state"] is True
    assert result["already_running"] is False


def test_dead_instance_error_is_clean(
    isolated_db, roms_dir: Path
) -> None:
    class DumpBackend(FakeInstanceBackend):
        def send_input(self, handle, steps, screenshot_mode, *, timeout=30):  # noqa: ARG002
            raise RuntimeError(
                "Error response from daemon:\n" + ("SECRET_DOCKER_DUMP\n" * 80)
            )

    manager = SessionManager(
        backend=DumpBackend(FakePyBoy),
        idle_timeout_seconds=30,
    )
    try:
        name, rom_path = _write_mapped_rom(roms_dir)
        manager.load("owner@example.com", name, rom_path)
        sent = manager.send_input("owner@example.com", name, ["a"])
        assert sent["sent"] is False
        assert "SECRET_DOCKER_DUMP" not in sent["error"]
        assert "daemon" not in sent["error"].lower()
        assert "\n" not in sent["error"]
    finally:
        manager.shutdown()


def test_input_after_backend_marks_instance_dead(
    isolated_db, roms_dir: Path
) -> None:
    class DyingBackend(FakeInstanceBackend):
        def is_running(self, handle):  # noqa: ARG002
            return False

        def close_reason(self, handle):  # noqa: ARG002
            return "idle_timeout"

    manager = SessionManager(
        backend=DyingBackend(FakePyBoy),
        idle_timeout_seconds=30,
    )
    try:
        name, rom_path = _write_mapped_rom(roms_dir)
        # start() still creates a handle; is_running then reports dead
        manager.load("owner@example.com", name, rom_path)
        sent = manager.send_input("owner@example.com", name, ["a"])
        assert sent["sent"] is False
        assert "idle timeout" in sent["error"]
        assert "SECRET" not in sent.get("error", "")
    finally:
        manager.shutdown()


def _docker_image_present(name: str) -> bool:
    try:
        probe = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=15,
        )
        if probe.returncode != 0:
            return False
        inspect = subprocess.run(
            ["docker", "image", "inspect", name],
            capture_output=True,
            timeout=15,
        )
        return inspect.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker
def test_docker_play_instance_save_survives_rm(
    isolated_db, roms_dir: Path
) -> None:
    from gb_mcp import config
    from gb_mcp.emulator.instance import DockerInstanceBackend, play_container_name

    if not _docker_image_present(config.INSTANCE_IMAGE):
        pytest.skip(f"{config.INSTANCE_IMAGE} is not built or Docker is unavailable")

    name = "1" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    rom_path = dest / "game.gb"
    rom_path.write_bytes(make_rom(size=32 * 1024, title=b"TESTGAME"))
    container = play_container_name(name)
    manager = SessionManager(
        backend=DockerInstanceBackend(),
        idle_timeout_seconds=30,
    )
    try:
        result = manager.load("owner@example.com", name, rom_path)
        assert result.get("started") is True, result.get("error")
        assert result["running"] is True
        sent = manager.send_input("owner@example.com", name, ["a"])
        assert sent.get("sent") is True, sent.get("error")
        assert sent["screenshot_count"] == 1
        assert sent["pngs"][0].startswith(PNG_MAGIC)
        stopped = manager.stop("owner@example.com", name)
        assert stopped.get("stopped") is True, stopped.get("error")
        inspect_after = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            timeout=15,
        )
        assert inspect_after.returncode != 0
        state = _state_path_for_rom(rom_path)
        assert state.is_file() and state.stat().st_size > 0

        again = manager.load("owner@example.com", name, rom_path)
        assert again["started"] is True
        assert again.get("restored_state") is True
        manager.stop("owner@example.com", name)
    finally:
        manager.shutdown()
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
