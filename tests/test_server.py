from __future__ import annotations

import base64
from pathlib import Path

import pytest

import db
import server
from gb_mcp import config

from rom_builder import make_rom


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
