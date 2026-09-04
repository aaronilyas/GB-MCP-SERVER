from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.utilities.types import Image

import db
import server
from gb_mcp import config
from gb_mcp.emulator.session import MAX_HOLD_FRAMES, MAX_INPUT_STEPS

from rom_builder import make_rom

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _unwrap_input(result: dict[str, Any] | list[Any]) -> tuple[dict[str, Any], list[Image]]:
    if isinstance(result, dict):
        return result, []
    status, *rest = result
    assert isinstance(status, dict)
    images = []
    for item in rest:
        assert isinstance(item, Image)
        images.append(item)
    return status, images


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "_docker_available", lambda: None)
    monkeypatch.setattr(server, "_ensure_image", lambda: None)
    monkeypatch.setattr(server, "_create_isolated_container", lambda: "cid")
    monkeypatch.setattr(server, "_destroy_container", lambda _cid: None)
    return monkeypatch


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_submit_rejects_invalid_base64() -> None:
    result = server.submit_gb_rom("not base64!!!")
    assert result["accepted"] is False
    assert result["saved"] is False
    assert "invalid base64" in result["error"]


def test_submit_rejects_empty_payload() -> None:
    result = server.submit_gb_rom(_b64(b""))
    assert result["accepted"] is False
    assert "empty" in result["error"]


def test_submit_rejects_oversized_encoded_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_ROM_B64_CHARS", 8)
    result = server.submit_gb_rom("A" * 9)
    assert result["accepted"] is False
    assert "maximum encoded size" in result["error"]


def test_submit_rejects_invalid_rom(fake_docker, isolated_db, roms_dir: Path) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": False, "reason": "Nintendo logo mismatch"},
    )
    result = server.submit_gb_rom(_b64(make_rom()))
    assert result["accepted"] is False
    assert result["saved"] is False
    assert result["subdirectory"] is None
    assert result["validation"]["reason"] == "Nintendo logo mismatch"
    assert list(roms_dir.iterdir()) == []


def test_submit_saves_and_requests_email(fake_docker, isolated_db, roms_dir: Path) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": True, "reason": "ok"},
    )
    rom = make_rom()
    result = server.submit_gb_rom(_b64(rom), filename="tetris.gb")
    assert result["accepted"] is True
    assert result["saved"] is True
    assert result["mapped"] is False
    assert result["subdirectory"]
    assert len(result["subdirectory"]) == db.SUBDIRECTORY_NAME_LENGTH
    assert "model_request" in result
    saved = config.ROOT / result["path"]
    assert saved.read_bytes() == rom
    assert saved.name == "tetris.gb"


def test_submit_maps_email_when_provided(fake_docker, isolated_db, roms_dir: Path) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": True, "reason": "ok"},
    )
    result = server.submit_gb_rom(_b64(make_rom()), email=" Owner@Example.com ")
    assert result["accepted"] is True
    assert result["mapped"] is True
    assert result["email"] == "owner@example.com"
    assert "model_request" not in result


def test_submit_destroys_container_on_validation_error(fake_docker, isolated_db, roms_dir: Path) -> None:
    destroyed: list[str] = []
    fake_docker.setattr(server, "_create_isolated_container", lambda: "cid-9")
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, _data: (_ for _ in ()).throw(RuntimeError("exec failed")),
    )
    fake_docker.setattr(server, "_destroy_container", lambda cid: destroyed.append(cid))
    result = server.submit_gb_rom(_b64(make_rom()))
    assert result["accepted"] is False
    assert destroyed == ["cid-9"]


def test_map_subdirectory_rejects_non_hex(roms_dir: Path) -> None:
    result = server.map_subdirectory_to_email("not-hex", "a@example.com")
    assert result["mapped"] is False
    assert "hexadecimal" in result["error"]


def test_map_subdirectory_requires_directory(isolated_db, roms_dir: Path) -> None:
    name = "a" * db.SUBDIRECTORY_NAME_LENGTH
    result = server.map_subdirectory_to_email(name, "a@example.com")
    assert result["mapped"] is False
    assert "does not exist" in result["error"]


def test_map_subdirectory_success(isolated_db, roms_dir: Path) -> None:
    name = "b" * db.SUBDIRECTORY_NAME_LENGTH
    (roms_dir / name).mkdir()
    result = server.map_subdirectory_to_email(name, "User@Example.com")
    assert result == {"mapped": True, "subdirectory": name, "email": "user@example.com"}


def test_list_subdirectories_for_email(isolated_db, roms_dir: Path) -> None:
    name = "c" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    (dest / "tetris.gb").write_bytes(make_rom(title=b"TETRIS"))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")

    result = server.list_subdirectories_for_email("Owner@Example.com")
    assert result["email"] == "owner@example.com"
    assert result["count"] == 1
    info = result["subdirectories"][0]
    assert info["subdirectory"] == name
    assert info["games"][0]["title"] == "TETRIS"


def test_list_invalid_email() -> None:
    result = server.list_subdirectories_for_email("not-an-email")
    assert result["count"] == 0
    assert result["subdirectories"] == []
    assert "error" in result


def _mapped_rom(roms_dir: Path, *, email: str = "owner@example.com", name: str | None = None) -> str:
    name = name or ("e" * db.SUBDIRECTORY_NAME_LENGTH)
    dest = roms_dir / name
    dest.mkdir()
    (dest / "tetris.gb").write_bytes(make_rom(title=b"TETRIS"))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, email)
    return name


def test_load_subdirectory_rom_requires_email_and_mapping(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    missing = server.load_subdirectory_rom("other@example.com", name)
    assert missing["started"] is False
    assert "not mapped" in missing["error"]

    invalid = server.load_subdirectory_rom("not-an-email", name)
    assert invalid["started"] is False
    assert "invalid email" in invalid["error"]

    bad_name = server.load_subdirectory_rom("owner@example.com", "nope")
    assert bad_name["started"] is False
    assert "hexadecimal" in bad_name["error"]


def test_load_subdirectory_rom_starts_pyboy(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    result = server.load_subdirectory_rom("Owner@Example.com", name)
    assert result["started"] is True
    assert result["running"] is True
    assert result["email"] == "owner@example.com"
    assert result["subdirectory"] == name
    assert result["rom"] == "tetris.gb"
    assert result["idle_timeout_seconds"] == 30

    again = server.load_subdirectory_rom("owner@example.com", name)
    assert again["already_running"] is True

    sent, images = _unwrap_input(
        server.send_pyboy_input("owner@example.com", name, ["A", "Up"], hold_frames=3)
    )
    assert sent["sent"] is True
    assert sent["steps"] == [{"buttons": ["a", "up"], "hold_frames": 3, "step_index": 0}]
    assert sent["screenshot_count"] == 1
    assert len(images) == 1
    assert images[0].data is not None and images[0].data.startswith(PNG_MAGIC)

    stopped = server.stop_pyboy("owner@example.com", name)
    assert stopped["stopped"] is True
    assert stopped["saved"] is True


def test_load_subdirectory_rom_requires_rom_file(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = "f" * db.SUBDIRECTORY_NAME_LENGTH
    (roms_dir / name).mkdir()
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")
    result = server.load_subdirectory_rom("owner@example.com", name)
    assert result["started"] is False
    assert "no Game Boy ROM" in result["error"]


def test_send_pyboy_input_validates_buttons(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name)
    empty = server.send_pyboy_input("owner@example.com", name, [])
    assert empty["sent"] is False
    assert "at least one button" in empty["error"]

    bad = server.send_pyboy_input("owner@example.com", name, ["turbo"])
    assert bad["sent"] is False
    assert "invalid button" in bad["error"]

    hold = server.send_pyboy_input("owner@example.com", name, ["a"], hold_frames=0)
    assert hold["sent"] is False
    assert "hold_frames" in hold["error"]


def test_send_pyboy_input_without_session(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    result, images = _unwrap_input(server.send_pyboy_input("owner@example.com", name, ["a"]))
    assert result["sent"] is False
    assert "no PyBoy session" in result["error"]
    assert images == []


def test_send_pyboy_input_unmapped_email_has_no_images(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir)
    result, images = _unwrap_input(
        server.send_pyboy_input("other@example.com", name, ["a"])
    )
    assert result["sent"] is False
    assert "not mapped" in result["error"]
    assert images == []


def test_send_pyboy_input_single_step_returns_one_image(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name)
    status, images = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com", name, ["a"], screenshot_mode="final"
        )
    )
    assert status["sent"] is True
    assert status["screenshot_mode"] == "final"
    assert status["screenshot_count"] == 1
    assert len(images) == 1
    assert images[0].data is not None and images[0].data.startswith(PNG_MAGIC)
    assert images[0].to_image_content().mime_type == "image/png"


def test_send_pyboy_input_steps_all_returns_three_images(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name)
    steps = [
        {"buttons": ["a"], "hold_frames": 1},
        {"buttons": ["b"], "hold_frames": 2},
        {"buttons": ["start"], "hold_frames": 1},
    ]
    status, images = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com", name, steps=steps, screenshot_mode="all"
        )
    )
    assert status["sent"] is True
    assert status["screenshot_count"] == 3
    assert status["screenshots"] == [
        {"step_index": 0},
        {"step_index": 1},
        {"step_index": 2},
    ]
    assert len(images) == 3
    payloads = []
    for image in images:
        assert image.data is not None and image.data.startswith(PNG_MAGIC)
        payloads.append(image.data)
    assert payloads[0] != payloads[1] != payloads[2]


def test_send_pyboy_input_steps_final_returns_one_image(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name)
    steps = [
        {"buttons": ["a"], "hold_frames": 1},
        {"buttons": ["b"], "hold_frames": 2},
        {"buttons": ["start"], "hold_frames": 1},
    ]
    status, images = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com", name, steps=steps, screenshot_mode="final"
        )
    )
    assert status["sent"] is True
    assert status["screenshot_count"] == 1
    assert status["screenshots"] == [{"step_index": 2}]
    assert len(images) == 1
    assert images[0].data is not None and images[0].data.startswith(PNG_MAGIC)


def test_send_pyboy_input_rejects_invalid_steps(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name)

    empty_steps, empty_images = _unwrap_input(
        server.send_pyboy_input("owner@example.com", name, steps=[])
    )
    assert empty_steps["sent"] is False
    assert "steps must not be empty" in empty_steps["error"]
    assert empty_images == []

    empty_in_step, _ = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com", name, steps=[{"buttons": [], "hold_frames": 1}]
        )
    )
    assert empty_in_step["sent"] is False
    assert "at least one button" in empty_in_step["error"]

    bad_button, _ = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com", name, steps=[{"buttons": ["turbo"]}]
        )
    )
    assert bad_button["sent"] is False
    assert "invalid button" in bad_button["error"]

    both, both_images = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com",
            name,
            ["a"],
            steps=[{"buttons": ["b"]}],
        )
    )
    assert both["sent"] is False
    assert "not both" in both["error"]
    assert both_images == []

    too_many, _ = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com",
            name,
            steps=[{"buttons": ["a"]}] * (MAX_INPUT_STEPS + 1),
        )
    )
    assert too_many["sent"] is False
    assert "steps" in too_many["error"]

    bad_mode, mode_images = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com", name, ["a"], screenshot_mode="none"
        )
    )
    assert bad_mode["sent"] is False
    assert "screenshot_mode" in bad_mode["error"]
    assert mode_images == []

    hold, _ = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com",
            name,
            steps=[{"buttons": ["a"], "hold_frames": MAX_HOLD_FRAMES + 1}],
        )
    )
    assert hold["sent"] is False
    assert "hold_frames" in hold["error"]


def test_stop_pyboy_without_session(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    result = server.stop_pyboy("owner@example.com", name)
    assert result["stopped"] is False
    assert "no PyBoy session" in result["error"]
