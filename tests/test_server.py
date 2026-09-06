from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as PILImage
from mcp.server.mcpserver.utilities.types import Image

import db
import server
from gb_mcp import config
from gb_mcp.emulator.play_limits import FORBIDDEN_RESPONSE_KEY_NEEDLES
from gb_mcp.http import oauth_token_claims
from gb_mcp.storage.roms import _state_path_for_rom
from gb_mcp.tools import ingest as ingest_mod

from rom_builder import make_rom

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_PUBLIC_TOOL_NAMES = (
    "add_rom",
    "list_games",
    "boot",
    "play",
    "save",
    "stop",
)

_FORBIDDEN_DESCRIPTION = (
    "blake2s",
    "battle_likely",
    "bearer token",
    "ping if you will think",
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
    monkeypatch.setattr(ingest_mod, "_docker_available", lambda: None)
    monkeypatch.setattr(ingest_mod, "_ensure_image", lambda: None)
    monkeypatch.setattr(ingest_mod, "_create_isolated_container", lambda: "cid")
    monkeypatch.setattr(ingest_mod, "_destroy_container", lambda _cid: None)
    return monkeypatch


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _mapped_rom(
    roms_dir: Path, *, email: str = "owner@example.com", name: str | None = None
) -> str:
    name = name or ("a" * db.SUBDIRECTORY_NAME_LENGTH)
    dest = roms_dir / name
    dest.mkdir()
    (dest / "tetris.gb").write_bytes(make_rom(title=b"TETRIS"))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, email)
    return name


def _as_owner():
    return oauth_token_claims({"email": "owner@example.com"})


def test_public_catalog_is_six_tools() -> None:
    tools = list(server.mcp._tool_manager.list_tools())
    names = [tool.name for tool in tools]
    assert len(names) == 6
    assert set(names) == set(_PUBLIC_TOOL_NAMES)
    blob = " ".join((tool.description or "") for tool in tools).lower()
    for needle in _FORBIDDEN_DESCRIPTION:
        assert needle not in blob, needle
    for old in (
        "submit_gb_rom",
        "begin_gb_rom_upload",
        "map_subdirectory_to_email",
        "list_subdirectories_for_email",
        "load_subdirectory_rom",
        "reset_pyboy",
        "send_pyboy_input",
        "ping_pyboy",
        "save_battery",
        "stop_pyboy",
    ):
        assert old not in names


def test_instructions_match_how_to_play() -> None:
    assert server.mcp.instructions == server.HOW_TO_PLAY
    assert server.how_to_play_resource() == server.HOW_TO_PLAY


def test_add_rom_rejects_invalid_base64() -> None:
    result = server.add_rom("not base64!!!")
    assert result["ok"] is False
    assert result["accepted"] is False
    assert "invalid base64" in result["error"]


def test_add_rom_rejects_empty_payload() -> None:
    result = server.add_rom(_b64(b""))
    assert result["ok"] is False
    assert "empty" in result["error"]


def test_add_rom_rejects_oversized_encoded_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MAX_ROM_B64_CHARS", 8)
    result = server.add_rom("A" * 9)
    assert result["ok"] is False
    assert "maximum encoded size" in result["error"]


def test_add_rom_without_identity_returns_model_request(
    fake_docker, isolated_db, roms_dir: Path
) -> None:
    result = server.add_rom(_b64(make_rom()), filename="tetris.gb")
    assert result["ok"] is False
    assert "model_request" in result
    assert result["model_request"]["name"] == "email"
    hex_dirs = [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert hex_dirs == []


def test_add_rom_maps_from_oauth(
    fake_docker, isolated_db, roms_dir: Path
) -> None:
    fake_docker.setattr(
        ingest_mod,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": True, "reason": "ok"},
    )
    with oauth_token_claims({"email": "Owner@Example.com"}):
        result = server.add_rom(_b64(make_rom()), filename="tetris.gb")
    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["mapped"] is True
    assert result["id"]
    assert "path" not in result
    assert "email" not in result
    with db.session_scope() as session:
        row = db.get_subdirectory_for_email(session, result["id"], "owner@example.com")
    assert row is not None


def test_add_rom_rejects_invalid_rom(fake_docker, isolated_db, roms_dir: Path) -> None:
    fake_docker.setattr(
        ingest_mod,
        "_validate_inside_container",
        lambda _cid, _data: {"valid": False, "reason": "bad header"},
    )
    with _as_owner():
        result = server.add_rom(_b64(make_rom()), filename="tetris.gb")
    assert result["ok"] is False
    assert result["accepted"] is False
    assert "bad header" in result["error"]


def test_add_rom_destroys_container_on_validation_error(
    fake_docker, isolated_db, roms_dir: Path
) -> None:
    destroyed: list[str] = []
    fake_docker.setattr(ingest_mod, "_create_isolated_container", lambda: "cid-1")
    fake_docker.setattr(
        ingest_mod, "_destroy_container", lambda cid: destroyed.append(cid)
    )
    fake_docker.setattr(
        ingest_mod,
        "_validate_inside_container",
        lambda _cid, _data: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with _as_owner():
        result = server.add_rom(_b64(make_rom()))
    assert result["ok"] is False
    assert destroyed == ["cid-1"]


def test_list_games_and_boot_play_stop(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = _mapped_rom(roms_dir)
    with _as_owner():
        listed = server.list_games()
        assert listed["games"][0]["title"] == "TETRIS"
        assert listed["games"][0]["id"] == name
        assert listed["games"][0]["playable"] is True
        assert set(listed["games"][0]) == {"title", "id", "playable"}

        booted = server.boot(title="tetris")
        assert booted["ok"] is True
        assert booted["stopped"] is False
        assert booted["game"] == "TESTGAME" or booted["game"] == "TETRIS"
        assert "email" not in booted
        assert "subdirectory" not in booted
        assert "region_hashes" not in booted

        status, images = _unwrap_input(server.play(buttons=["up"]))
        assert status["ok"] is True
        assert status["stopped"] is False
        assert "email" not in status
        assert "region_hashes" not in status
        assert "native_size" not in status
        assert len(images) == 1
        assert images[0].data is not None
        assert images[0].data.startswith(PNG_MAGIC)
        image = PILImage.open(io.BytesIO(images[0].data))
        assert image.size == (160 * 4, 144 * 4)

        saved = server.save()
        assert saved["ok"] is True
        assert _state_path_for_rom(roms_dir / name / "tetris.gb").is_file()

        stopped = server.stop()
        assert stopped["ok"] is True
        assert stopped["stopped"] is True


def test_play_after_boot_needs_no_email_or_id(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    _mapped_rom(roms_dir)
    with _as_owner():
        server.boot(title="TETRIS")
        status, images = _unwrap_input(server.play(buttons=["a"]))
    assert status["ok"] is True
    assert images


def test_boot_unknown_title(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    _mapped_rom(roms_dir)
    with _as_owner():
        result = server.boot(title="missing")
    assert result["ok"] is False
    assert "error" in result


def test_boot_reset_drops_snapshot(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    name = _mapped_rom(roms_dir)
    rom_path = roms_dir / name / "tetris.gb"
    state = _state_path_for_rom(rom_path)
    state.write_bytes(b"POISON")
    with _as_owner():
        result = server.boot(id=name, reset=True)
        status, _images = _unwrap_input(server.play(buttons=["a"]))
    assert result["ok"] is True
    assert not state.exists() or state.stat().st_size == 0
    assert status["ok"] is True


def test_play_without_session(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    _mapped_rom(roms_dir)
    with _as_owner():
        result, images = _unwrap_input(server.play(buttons=["a"]))
    assert result["ok"] is False
    assert images == []


def test_play_wait_empty_buttons(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    _mapped_rom(roms_dir)
    with _as_owner():
        server.boot(title="TETRIS")
        status, images = _unwrap_input(server.play(buttons=[]))
    assert status["ok"] is True
    assert len(images) == 1


def test_play_rejects_buttons_and_steps(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    _mapped_rom(roms_dir)
    with _as_owner():
        server.boot(title="TETRIS")
        result = server.play(buttons=["a"], steps=[{"buttons": ["b"]}])
    status, images = _unwrap_input(result)
    assert status["ok"] is False
    assert images == []
    assert "not both" in status["error"]


def test_stop_without_session(isolated_db, roms_dir: Path, pyboy_manager) -> None:
    with _as_owner():
        result = server.stop()
    assert result["ok"] is False
    assert result["stopped"] is True or "error" in result


def test_list_omitted_email_without_token_identity_returns_model_request() -> None:
    result = server.list_games()
    assert result["ok"] is False
    assert "model_request" in result
    assert result["model_request"]["name"] == "email"


def test_list_binds_email_from_oauth_email_claim(isolated_db, roms_dir: Path) -> None:
    name = _mapped_rom(roms_dir, email="owner@example.com")
    with oauth_token_claims({"email": "Owner@Example.com", "sub": "other@example.com"}):
        result = server.list_games()
    assert result["games"][0]["id"] == name
    assert "model_request" not in result
    assert "email" not in result


def test_list_binds_email_from_oauth_sub_claim(isolated_db, roms_dir: Path) -> None:
    name = _mapped_rom(roms_dir, email="sub-user@example.com")
    with oauth_token_claims({"sub": "Sub-User@example.com"}):
        result = server.list_games()
    assert result["games"][0]["id"] == name


def test_non_email_sub_claim_is_not_session_identity() -> None:
    with oauth_token_claims({"sub": "gb-mcp-user"}):
        result = server.list_games()
    assert "model_request" in result


def test_boot_truncated_rom_does_not_start_session(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    name = "b" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    (dest / "red.gb").write_bytes(make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")
    with _as_owner():
        result = server.boot(id=name)
    assert result["ok"] is False
    assert "error" in result
    assert pyboy_manager.get("owner@example.com") is None or not pyboy_manager.get(
        "owner@example.com"
    ).is_running


def test_public_play_status_does_not_leak(
    isolated_db, roms_dir: Path, pyboy_manager
) -> None:
    _mapped_rom(roms_dir)
    with _as_owner():
        server.boot(title="TETRIS")
        status, images = _unwrap_input(server.play(buttons=["a"], frames=8))
    assert set(status) <= {"ok", "frames", "stopped", "game", "looks_like", "error"}
    joined = " ".join(status.keys()).lower()
    for needle in FORBIDDEN_RESPONSE_KEY_NEEDLES:
        assert needle not in joined
    assert images
    blob = images[0].data
    assert blob is not None
    image = PILImage.open(io.BytesIO(blob))
    assert image.size == (640, 576)


def test_add_rom_header_only_pokemon_rejected(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        ingest_mod,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom(size=0x150, title=b"POKEMON RED", rom_size_code=0x05)
    with _as_owner():
        result = server.add_rom(_b64(rom), filename="pokemon.gb")
    assert result["ok"] is False
    assert result["accepted"] is False
