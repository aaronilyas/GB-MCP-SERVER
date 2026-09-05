from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.utilities.types import Image

import db
import server
from gb_mcp import config
from gb_mcp.emulator import session as pyboy_sessions
from gb_mcp.emulator.session import MAX_HOLD_FRAMES, MAX_INPUT_STEPS
from gb_mcp.http import oauth_token_claims
from gb_mcp.storage.roms import _state_path_for_rom

from rom_builder import make_rom

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_PUBLIC_TOOL_NAMES = (
    "submit_gb_rom",
    "begin_gb_rom_upload",
    "get_gb_rom_upload",
    "append_gb_rom_upload",
    "append_gb_rom_upload_batch",
    "finalize_gb_rom_upload",
    "abort_gb_rom_upload",
    "map_subdirectory_to_email",
    "list_subdirectories_for_email",
    "load_subdirectory_rom",
    "reset_pyboy",
    "send_pyboy_input",
    "ping_pyboy",
    "save_battery",
    "stop_pyboy",
)


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


def test_submit_replace_owned_truncated_mapping(
    fake_docker, isolated_db, roms_dir: Path, validator_module, pyboy_manager
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    name = "d" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    legacy = dest / "Pokemon_-_Red_Version_USA_Europe_.gb"
    legacy.write_bytes(make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")

    full = make_rom(title=b"POKEMON RED")
    result = server.submit_gb_rom(
        _b64(full),
        filename="red.gb",
        email="owner@example.com",
        subdirectory=name,
    )
    assert result["accepted"] is True
    assert result["subdirectory"] == name
    assert result["mapped"] is True
    saved = config.ROOT / result["path"]
    assert saved.read_bytes() == full
    assert not legacy.exists()
    hex_dirs = [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert [p.name for p in hex_dirs] == [name]
    listed = server.list_subdirectories_for_email("owner@example.com")
    game = listed["subdirectories"][0]["games"][0]
    assert game["playable"] is True
    assert game["size_bytes"] == len(full)
    loaded = server.load_subdirectory_rom("owner@example.com", name)
    assert loaded["started"] is True
    assert loaded["running"] is True


def test_submit_subdirectory_not_owned_does_not_write(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    name = "e" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    original = make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    (dest / "red.gb").write_bytes(original)
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")

    result = server.submit_gb_rom(
        _b64(make_rom()),
        filename="other.gb",
        email="intruder@example.com",
        subdirectory=name,
    )
    assert result["accepted"] is False
    assert result["saved"] is False
    assert "not mapped" in result["error"]
    assert (dest / "red.gb").read_bytes() == original
    assert list(dest.glob("*.gb")) == [dest / "red.gb"]
    hex_dirs = [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert [p.name for p in hex_dirs] == [name]


def test_submit_header_only_pokemon_rejected(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    result = server.submit_gb_rom(_b64(rom), filename="pokemon.gb")
    assert result["accepted"] is False
    assert result["saved"] is False
    assert "1024" in result["error"]
    assert "1048576" in result["error"]
    assert "0x05" in result["error"]
    hex_dirs = [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert hex_dirs == []


def test_submit_rejects_stub_even_if_stale_validator_says_valid(
    fake_docker, isolated_db, roms_dir: Path
) -> None:
    """Old validator images returned valid:true + size_note for 512-byte headers."""
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, _data: {
            "valid": True,
            "reason": "valid Game Boy ROM header",
            "title": "POKEMON RED",
            "size_bytes": 512,
            "cgb": False,
            "rom_size_code": 5,
            "size_note": "size 512 != header expectation 1048576",
            "header_checksum": "0x20",
        },
    )
    rom = make_rom(size=512, title=b"POKEMON RED", rom_size_code=0x05)
    result = server.submit_gb_rom(_b64(rom), filename="header-only.gb")
    assert result["accepted"] is False
    assert result["saved"] is False
    assert result["mapped"] is False
    validation = result["validation"]
    assert validation["valid"] is False
    assert "512" in validation["reason"]
    assert "1048576" in validation["reason"]
    assert validation.get("size_note") is None
    hex_dirs = [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert hex_dirs == []


@pytest.mark.parametrize("size", [512, 8192])
def test_submit_pokemon_header_slices_rejected(
    fake_docker, isolated_db, roms_dir: Path, validator_module, size: int
) -> None:
    """Incident JSON must not return valid:true for 512/8192-byte POKEMON RED slices."""
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom(size=size, title=b"POKEMON RED", rom_size_code=0x05)
    result = server.submit_gb_rom(_b64(rom), filename="header-only.gb")
    assert result["accepted"] is False
    assert result["saved"] is False
    assert result["mapped"] is False
    assert result["path"] is None
    assert result["subdirectory"] is None
    validation = result["validation"]
    assert validation["valid"] is False
    assert "truncated" in validation["reason"]
    assert str(size) in validation["reason"]
    assert "1048576" in validation["reason"]
    assert "0x05" in validation["reason"]
    assert validation.get("size_note") is None
    hex_dirs = [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert hex_dirs == []


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


def test_submit_boot_starts_pyboy(fake_docker, isolated_db, roms_dir: Path, pyboy_manager) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": True, "reason": "ok"},
    )
    result = server.submit_gb_rom(_b64(make_rom()), email="owner@example.com", boot=True)
    assert result["accepted"] is True
    assert result["mapped"] is True
    assert result.get("started") is True
    assert result.get("running") is True


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


def test_public_tool_name_list() -> None:
    names = [tool.name for tool in server.mcp._tool_manager.list_tools()]
    for name in _PUBLIC_TOOL_NAMES:
        assert name in names
    assert "reset_pyboy" in names
    reset = server.mcp._tool_manager.get_tool("reset_pyboy")
    description = reset.description.lower()
    assert "cold boot without the previous pyboy snapshot" in description
    assert "warp" not in description
    load = server.mcp._tool_manager.get_tool("load_subdirectory_rom")
    load_desc = load.description.lower()
    assert "restore_state" in load_desc
    assert "warp" not in load_desc


def test_load_subdirectory_rom_starts_pyboy(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    result = server.load_subdirectory_rom("Owner@Example.com", name)
    assert result["started"] is True
    assert result["running"] is True
    assert result["email"] == "owner@example.com"
    assert result["subdirectory"] == name
    assert result["rom"] == "tetris.gb"
    assert result["idle_timeout_seconds"] == 2700

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


def test_load_truncated_rom_does_not_start_session(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = "f" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    (dest / "red.gb").write_bytes(
        make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    )
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")

    started: list[object] = []
    original = pyboy_manager._backend.start

    def wrapped(*args: Any, **kwargs: Any):
        started.append(1)
        return original(*args, **kwargs)

    pyboy_manager._backend.start = wrapped  # type: ignore[method-assign]
    result = server.load_subdirectory_rom("owner@example.com", name)
    assert result["started"] is False
    assert result["running"] is False
    assert "1024" in result["error"]
    assert "1048576" in result["error"]
    assert "0x05" in result["error"]
    assert "finalize_gb_rom_upload" in result["error"]
    assert f"subdirectory={name}" in result["error"]
    assert started == []
    assert pyboy_manager.get("owner@example.com") is None


def test_list_exposes_playable_false_for_truncated(
    isolated_db, roms_dir: Path
) -> None:
    name = "c" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    (dest / "red.gb").write_bytes(
        make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    )
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")
    result = server.list_subdirectories_for_email("owner@example.com")
    info = result["subdirectories"][0]
    game = info["games"][0]
    assert game["playable"] is False
    assert "1024" in game["unplayable_reason"]
    assert "1048576" in game["unplayable_reason"]
    rom_file = next(e for e in info["files"] if e["filename"] == "red.gb")
    assert rom_file["playable"] is False
    assert "1024" in rom_file["unplayable_reason"]
    assert "1048576" in rom_file["unplayable_reason"]


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
    assert [item.get("step_index") for item in status["screenshots"]] == [0, 1, 2]
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
    assert status["screenshots"][-1].get("step_index") == 2
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

    empty_in_step, wait_images = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com", name, steps=[{"buttons": [], "hold_frames": 1}]
        )
    )
    assert empty_in_step["sent"] is True
    assert empty_in_step["steps"][0]["buttons"] == []
    assert wait_images

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


def test_load_subdirectory_rom_restore_state_false_skips_snapshot(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir)
    rom_path = roms_dir / name / "tetris.gb"
    _state_path_for_rom(rom_path).write_bytes(b"POISON")
    result = server.load_subdirectory_rom(
        "owner@example.com", name, restore_state=False
    )
    assert result["started"] is True
    assert result["running"] is True
    assert result["restored_state"] is False
    session = pyboy_sessions.manager.get("owner@example.com")
    assert session is not None
    assert session._pyboy.loaded_state is None
    assert _state_path_for_rom(rom_path).read_bytes() == b"POISON"


def test_reset_pyboy_drops_poisoned_snapshot(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir)
    rom_path = roms_dir / name / "tetris.gb"
    _state_path_for_rom(rom_path).write_bytes(b"POISON")
    loaded = server.load_subdirectory_rom("owner@example.com", name)
    assert loaded["restored_state"] is True
    first = pyboy_sessions.manager.get("owner@example.com")
    assert first is not None
    first_pyboy = first._pyboy

    result = server.reset_pyboy("owner@example.com", name)
    assert result["started"] is True
    assert result["running"] is True
    assert result["already_running"] is False
    assert result["restored_state"] is False
    assert result["discarded"] is True
    assert result["email"] == "owner@example.com"
    assert not _state_path_for_rom(rom_path).exists()
    session = pyboy_sessions.manager.get("owner@example.com")
    assert session is not None
    assert session._pyboy is not first_pyboy
    assert session._pyboy.loaded_state is None
    sent, images = _unwrap_input(server.send_pyboy_input("owner@example.com", name, ["a"]))
    assert sent["sent"] is True
    assert images
    assert images[0].data is not None and images[0].data.startswith(PNG_MAGIC)
    from gb_mcp.emulator.play_limits import FORBIDDEN_RESPONSE_KEY_NEEDLES

    joined = " ".join(_flatten_status_keys(result)).lower()
    for needle in FORBIDDEN_RESPONSE_KEY_NEEDLES:
        assert needle not in joined


def test_reset_pyboy_omitted_email_without_identity_returns_model_request() -> None:
    result = server.reset_pyboy(subdirectory="a" * db.SUBDIRECTORY_NAME_LENGTH)
    assert result["started"] is False
    assert result["running"] is False
    assert "model_request" in result
    assert result["model_request"]["name"] == "email"
    assert "invent" in result["model_request"]["instruction"].lower()
    assert "trainer@x.ai" not in str(result).lower()


def test_ping_pyboy_does_not_tick(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name, idle_timeout_seconds=30)
    session = pyboy_sessions.manager.get("owner@example.com")
    assert session is not None
    ticks = session._pyboy.ticks
    result = server.ping_pyboy("owner@example.com", name)
    assert result["alive"] is True
    assert session._pyboy.ticks == ticks
    assert result["seconds_since_last_input"] < 1


def test_save_battery_keeps_session(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name, idle_timeout_seconds=30)
    result = server.save_battery("owner@example.com", name)
    assert result["saved"] is True
    sent, images = _unwrap_input(server.send_pyboy_input("owner@example.com", name, ["a"]))
    assert sent["sent"] is True
    assert images


def test_send_pyboy_input_hold_and_scale(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    from gb_mcp.emulator.play_limits import FORBIDDEN_RESPONSE_KEY_NEEDLES
    from PIL import Image as PILImage
    import io

    name = _mapped_rom(roms_dir)
    server.load_subdirectory_rom("owner@example.com", name, idle_timeout_seconds=30)
    status, images = _unwrap_input(
        server.send_pyboy_input(
            "owner@example.com",
            name,
            macro="hold",
            buttons=["up"],
            max_frames=12,
            disable_default_hold_abort=True,
            screenshot_scale=3,
            screenshot_mode="final",
        )
    )
    assert status["sent"] is True
    assert status["emulation_speed"] == 0
    assert status["screenshot_scale"] == 3
    assert status["native_size"] == [160, 144]
    assert "stop_reason" in status
    assert "region_hashes" in status and "full" in status["region_hashes"]
    assert "classifiers" in status
    blob = images[0].data
    assert blob is not None
    image = PILImage.open(io.BytesIO(blob))
    assert image.size == (160 * 3, 144 * 3)
    joined = " ".join(_flatten_status_keys(status)).lower()
    for needle in FORBIDDEN_RESPONSE_KEY_NEEDLES:
        assert needle not in joined


def test_list_omitted_email_without_token_identity_returns_model_request() -> None:
    result = server.list_subdirectories_for_email()
    assert result["count"] == 0
    assert result["subdirectories"] == []
    assert "model_request" in result
    assert result["model_request"]["name"] == "email"
    assert "invent" in result["model_request"]["instruction"].lower() or "ask the user" in result[
        "model_request"
    ]["instruction"].lower()


def test_list_binds_email_from_oauth_email_claim(isolated_db, roms_dir: Path) -> None:
    name = _mapped_rom(roms_dir, email="owner@example.com")
    with oauth_token_claims({"email": "Owner@Example.com", "sub": "other@example.com"}):
        result = server.list_subdirectories_for_email()
    assert result["email"] == "owner@example.com"
    assert result["count"] == 1
    assert result["subdirectories"][0]["subdirectory"] == name
    assert "model_request" not in result


def test_list_binds_email_from_oauth_sub_claim(isolated_db, roms_dir: Path) -> None:
    name = _mapped_rom(roms_dir, email="sub-user@example.com")
    with oauth_token_claims({"sub": "Sub-User@example.com"}):
        result = server.list_subdirectories_for_email()
    assert result["email"] == "sub-user@example.com"
    assert result["count"] == 1
    assert result["subdirectories"][0]["subdirectory"] == name


def test_list_explicit_email_overrides_oauth_claims(isolated_db, roms_dir: Path) -> None:
    name = _mapped_rom(roms_dir, email="owner@example.com")
    with oauth_token_claims({"email": "token@example.com"}):
        listed_explicit = server.list_subdirectories_for_email("owner@example.com")
        listed_token = server.list_subdirectories_for_email()
    assert listed_explicit["email"] == "owner@example.com"
    assert listed_explicit["count"] == 1
    assert listed_explicit["subdirectories"][0]["subdirectory"] == name
    assert listed_token["email"] == "token@example.com"
    assert listed_token["count"] == 0


def test_non_email_sub_claim_is_not_session_identity() -> None:
    with oauth_token_claims({"sub": "gb-mcp-user"}):
        result = server.list_subdirectories_for_email()
    assert "model_request" in result
    assert result["count"] == 0


def test_load_binds_email_from_oauth_claims(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir, email="owner@example.com")
    with oauth_token_claims({"email": "owner@example.com"}):
        loaded = server.load_subdirectory_rom(subdirectory=name)
        sent, images = _unwrap_input(
            server.send_pyboy_input(subdirectory=name, buttons=["a"])
        )
        pinged = server.ping_pyboy(subdirectory=name)
        saved = server.save_battery(subdirectory=name)
        reset = server.reset_pyboy(subdirectory=name)
        stopped = server.stop_pyboy(subdirectory=name)
    assert loaded["started"] is True
    assert loaded["email"] == "owner@example.com"
    assert sent["sent"] is True
    assert images
    assert pinged["alive"] is True
    assert saved["saved"] is True
    assert reset["started"] is True
    assert reset["email"] == "owner@example.com"
    assert reset["restored_state"] is False
    assert stopped["stopped"] is True


def test_load_explicit_email_overrides_oauth_claims(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir, email="owner@example.com")
    with oauth_token_claims({"email": "token@example.com"}):
        denied = server.load_subdirectory_rom(subdirectory=name)
        loaded = server.load_subdirectory_rom("owner@example.com", name)
    assert denied["started"] is False
    assert "not mapped" in denied["error"]
    assert loaded["started"] is True
    assert loaded["email"] == "owner@example.com"


def test_map_subdirectory_binds_from_oauth_claims(isolated_db, roms_dir: Path) -> None:
    name = "b" * db.SUBDIRECTORY_NAME_LENGTH
    (roms_dir / name).mkdir()
    with oauth_token_claims({"email": "User@Example.com"}):
        result = server.map_subdirectory_to_email(name)
    assert result == {"mapped": True, "subdirectory": name, "email": "user@example.com"}


def test_map_subdirectory_omitted_email_without_identity_returns_model_request(
    isolated_db, roms_dir: Path
) -> None:
    name = "b" * db.SUBDIRECTORY_NAME_LENGTH
    (roms_dir / name).mkdir()
    result = server.map_subdirectory_to_email(name)
    assert result["mapped"] is False
    assert result["model_request"]["name"] == "email"
    assert result["subdirectory"] == name


def test_submit_maps_from_oauth_email_claim(
    fake_docker, isolated_db, roms_dir: Path
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": True, "reason": "ok"},
    )
    with oauth_token_claims({"email": "oauth@example.com"}):
        result = server.submit_gb_rom(_b64(make_rom()), filename="tetris.gb")
    assert result["accepted"] is True
    assert result["mapped"] is True
    assert result["email"] == "oauth@example.com"
    assert "model_request" not in result


def _flatten_status_keys(payload: object, prefix: str = "") -> list[str]:
    keys: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.append(path)
            keys.extend(_flatten_status_keys(value, path))
    elif isinstance(payload, list):
        for item in payload[:8]:
            keys.extend(_flatten_status_keys(item, prefix))
    return keys
