from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import db
from gb_mcp import config
from gb_mcp.http import oauth_token_claims
from gb_mcp.storage import uploads as upload_store
from gb_mcp.storage.roms import _rom_in_subdirectory
from gb_mcp.tools import ingest as ingest_mod

from rom_builder import make_rom


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ingest_mod, "_docker_available", lambda: None)
    monkeypatch.setattr(ingest_mod, "_ensure_image", lambda: None)
    monkeypatch.setattr(ingest_mod, "_create_isolated_container", lambda: "cid")
    monkeypatch.setattr(ingest_mod, "_destroy_container", lambda _cid: None)
    return monkeypatch


def _hex_rom_dirs(roms_dir: Path) -> list[Path]:
    return [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]


def _slices(rom: bytes, chunk_size: int) -> list[bytes]:
    return [rom[offset : offset + chunk_size] for offset in range(0, len(rom), chunk_size)]


def _chunked_upload(rom: bytes, filename: str, email: str | None = None) -> str:
    digest = hashlib.sha256(rom).hexdigest()
    begun = upload_store.begin_upload(
        filename=filename, total_bytes=len(rom), sha256=digest, email=email
    )
    chunk_size = begun["chunk_size"]
    upload_id = begun["upload_id"]
    offset = 0
    index = 0
    while offset < len(rom):
        piece = rom[offset : offset + chunk_size]
        appended = upload_store.append_chunk(
            upload_id=upload_id, chunk_index=index, chunk_base64=__import__("base64").b64encode(piece).decode()
        )
        assert appended["next_index"] == index + 1
        offset += len(piece)
        index += 1
    return upload_id


def test_chunked_upload_persists_and_maps(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    order: list[str] = []

    def create() -> str:
        order.append("create")
        return "cid"

    def validate(cid: str, data: bytes) -> dict:
        order.append("validate")
        assert cid == "cid"
        return validator_module.validate_gb_rom_bytes(data)

    fake_docker.setattr(ingest_mod, "_create_isolated_container", create)
    fake_docker.setattr(ingest_mod, "_validate_inside_container", validate)

    rom = make_rom(rom_size_code=0x01)
    upload_id = _chunked_upload(rom, "game.gb", email="owner@example.com")
    result = ingest_mod.finalize_staged(upload_id, email="owner@example.com")
    assert result["accepted"] is True
    assert result["saved"] is True
    assert result["mapped"] is True
    saved = config.ROOT / result["path"]
    assert saved.read_bytes() == rom
    assert order == ["create", "validate"]
    staging = roms_dir / ".uploads" / upload_id
    assert not staging.exists()
    assert len(_hex_rom_dirs(roms_dir)) == 1


def test_wrong_sha256_does_not_persist(isolated_db, roms_dir: Path) -> None:
    rom = make_rom()
    begun = upload_store.begin_upload(
        filename="game.gb", total_bytes=len(rom), sha256="0" * 64
    )
    upload_id = begun["upload_id"]
    import base64

    for index, piece in enumerate(_slices(rom, begun["chunk_size"])):
        upload_store.append_chunk(
            upload_id=upload_id,
            chunk_index=index,
            chunk_base64=base64.b64encode(piece).decode(),
        )
    result = ingest_mod.finalize_staged(upload_id, email="owner@example.com")
    assert result["accepted"] is False
    assert "sha256" in result["error"]
    assert _hex_rom_dirs(roms_dir) == []


def test_missing_chunk_rejected(isolated_db, roms_dir: Path) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = upload_store.begin_upload(
        filename="game.gb", total_bytes=len(rom), sha256=digest
    )
    import base64

    with pytest.raises(ValueError):
        upload_store.append_chunk(
            upload_id=begun["upload_id"],
            chunk_index=1,
            chunk_base64=base64.b64encode(rom[: begun["chunk_size"]]).decode(),
        )


def test_get_upload_status_after_two_appends(isolated_db, roms_dir: Path) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = upload_store.begin_upload(
        filename="game.gb", total_bytes=len(rom), sha256=digest
    )
    import base64

    chunk_size = begun["chunk_size"]
    upload_id = begun["upload_id"]
    first, second = rom[:chunk_size], rom[chunk_size : chunk_size * 2]
    upload_store.append_chunk(
        upload_id=upload_id, chunk_index=0, chunk_base64=base64.b64encode(first).decode()
    )
    upload_store.append_chunk(
        upload_id=upload_id, chunk_index=1, chunk_base64=base64.b64encode(second).decode()
    )
    status = upload_store.get_upload(upload_id)
    assert status["next_index"] == 2
    assert status["received_bytes"] == len(first) + len(second)


def test_abort_deletes_staging(isolated_db, roms_dir: Path) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = upload_store.begin_upload(
        filename="game.gb", total_bytes=len(rom), sha256=digest
    )
    upload_id = begun["upload_id"]
    dest = roms_dir / ".uploads" / upload_id
    assert dest.is_dir()
    upload_store.abort_upload(upload_id)
    assert not dest.exists()
    again = upload_store.abort_upload(upload_id)
    assert again["upload_id"] == upload_id
    assert _hex_rom_dirs(roms_dir) == []


def test_expire_uploads_removes_stale(roms_dir: Path, monkeypatch) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = upload_store.begin_upload(
        filename="game.gb", total_bytes=len(rom), sha256=digest
    )
    dest = roms_dir / ".uploads" / begun["upload_id"]
    assert dest.is_dir()
    monkeypatch.setattr(config, "ROM_UPLOAD_TTL_SECONDS", 0)
    upload_store.expire_uploads()
    assert not dest.exists()


def test_list_games_reclaims_expired_uploads(
    isolated_db, roms_dir: Path, monkeypatch
) -> None:
    import server

    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = upload_store.begin_upload(
        filename="game.gb", total_bytes=len(rom), sha256=digest
    )
    dest = roms_dir / ".uploads" / begun["upload_id"]
    assert dest.is_dir()
    monkeypatch.setattr(config, "ROM_UPLOAD_TTL_SECONDS", 0)
    with oauth_token_claims({"email": "owner@example.com"}):
        listed = server.list_games()
    assert listed["games"] == []
    assert not dest.exists()


def test_add_rom_still_works_for_small_rom(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    import server

    fake_docker.setattr(
        ingest_mod,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom()
    with oauth_token_claims({"email": "owner@example.com"}):
        result = server.add_rom(
            __import__("base64").b64encode(rom).decode(), filename="homebrew.gb"
        )
    assert result["accepted"] is True
    assert result["mapped"] is True
    saved = _rom_in_subdirectory(result["id"])
    assert saved.read_bytes() == rom
    assert saved.name == "homebrew.gb"


def test_finalize_replace_owned_mapping(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        ingest_mod,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    name = "c" * db.SUBDIRECTORY_NAME_LENGTH
    dest = roms_dir / name
    dest.mkdir()
    truncated = dest / "old.gb"
    truncated.write_bytes(make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05))
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "owner@example.com")
    full = make_rom(title=b"POKEMON RED")
    upload_id = _chunked_upload(full, "red.gb", email="owner@example.com")
    result = ingest_mod.finalize_staged(
        upload_id, email="owner@example.com", subdirectory=name
    )
    assert result["accepted"] is True
    assert result["id"] == name
    chosen = _rom_in_subdirectory(name)
    assert chosen.read_bytes() == full
    assert not truncated.exists()
