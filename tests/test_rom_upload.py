from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

import db
import server
from gb_mcp import config
from gb_mcp.gb.constants import MAX_ROM_BYTES
from gb_mcp.storage import uploads as upload_store
from gb_mcp.storage.roms import _rom_in_subdirectory

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


def _hex_rom_dirs(roms_dir: Path) -> list[Path]:
    return [p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]


def _upload_meta(roms_dir: Path, upload_id: str) -> dict:
    path = roms_dir / ".uploads" / upload_id / "meta.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _slices(rom: bytes, chunk_size: int) -> list[bytes]:
    return [rom[offset : offset + chunk_size] for offset in range(0, len(rom), chunk_size)]


def _chunked_upload(rom: bytes, filename: str, email: str | None = None) -> str:
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload(filename, len(rom), digest, email=email)
    assert begun["started"] is True
    chunk_size = begun["chunk_size"]
    upload_id = begun["upload_id"]
    offset = 0
    index = 0
    while offset < len(rom):
        piece = rom[offset : offset + chunk_size]
        appended = server.append_gb_rom_upload(upload_id, index, _b64(piece))
        assert appended["appended"] is True
        offset += len(piece)
        index += 1
    return upload_id


def _map_truncated(
    roms_dir: Path,
    *,
    email: str = "owner@example.com",
    name: str | None = None,
    filename: str = "Pokemon_-_Red_Version_USA_Europe_.gb",
) -> str:
    name = name or ("c" * db.SUBDIRECTORY_NAME_LENGTH)
    dest = roms_dir / name
    dest.mkdir()
    (dest / filename).write_bytes(
        make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    )
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, email)
    return name


def test_chunked_upload_persists_and_maps(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom(rom_size_code=0x01)  # 64 KiB, eight 8 KiB chunks
    digest = hashlib.sha256(rom).hexdigest()
    order: list[str] = []

    def create() -> str:
        order.append("create")
        return "cid"

    def validate(cid: str, data: bytes) -> dict:
        order.append("validate")
        assert cid == "cid"
        return validator_module.validate_gb_rom_bytes(data)

    fake_docker.setattr(server, "_create_isolated_container", create)
    fake_docker.setattr(server, "_validate_inside_container", validate)

    begun = server.begin_gb_rom_upload(
        "game.gb", len(rom), digest, email="owner@example.com"
    )
    assert begun["started"] is True
    chunk_size = begun["chunk_size"]
    assert chunk_size == 8 * 1024
    upload_id = begun["upload_id"]
    assert len(upload_id) == 32

    full_b64 = _b64(rom)
    index = 0
    offset = 0
    next_index = 0
    while offset < len(rom):
        piece = rom[offset : offset + chunk_size]
        chunk_b64 = _b64(piece)
        assert len(chunk_b64) < len(full_b64)
        appended = server.append_gb_rom_upload(upload_id, index, chunk_b64)
        assert appended["appended"] is True
        next_index = appended["next_index"]
        offset += len(piece)
        index += 1
    assert next_index == index

    result = server.finalize_gb_rom_upload(upload_id, email="owner@example.com")
    assert result["accepted"] is True
    assert result["saved"] is True
    assert result["mapped"] is True
    assert result["email"] == "owner@example.com"
    saved = config.ROOT / result["path"]
    assert saved.read_bytes() == rom
    assert order == ["create", "validate"]
    staging = roms_dir / ".uploads" / upload_id
    assert not staging.exists()
    hex_dirs = _hex_rom_dirs(roms_dir)
    assert len(hex_dirs) == 1


def test_wrong_sha256_does_not_persist(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom()
    begun = server.begin_gb_rom_upload("game.gb", len(rom), "0" * 64)
    upload_id = begun["upload_id"]
    for index, piece in enumerate(_slices(rom, begun["chunk_size"])):
        appended = server.append_gb_rom_upload(upload_id, index, _b64(piece))
        assert appended["appended"] is True
    result = server.finalize_gb_rom_upload(upload_id)
    assert result["accepted"] is False
    assert "sha256" in result["error"]
    assert _hex_rom_dirs(roms_dir) == []


def test_missing_chunk_rejected(fake_docker, isolated_db, roms_dir: Path) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload("game.gb", len(rom), digest)
    hole = server.append_gb_rom_upload(begun["upload_id"], 1, _b64(rom[:100]))
    assert hole["appended"] is False
    assert "chunk_index" in hole["error"]
    assert _hex_rom_dirs(roms_dir) == []


def test_oversize_total_rejected(isolated_db, roms_dir: Path) -> None:
    result = server.begin_gb_rom_upload("game.gb", MAX_ROM_BYTES + 1, "a" * 64)
    assert result["started"] is False
    assert "maximum size" in result["error"]
    assert _hex_rom_dirs(roms_dir) == []


def test_incomplete_then_finalize_does_not_persist(
    fake_docker, isolated_db, roms_dir: Path
) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload("game.gb", len(rom), digest)
    one_kib = rom[:1024]
    appended = server.append_gb_rom_upload(begun["upload_id"], 0, _b64(one_kib))
    assert appended["appended"] is True
    result = server.finalize_gb_rom_upload(begun["upload_id"])
    assert result["accepted"] is False
    assert "incomplete" in result["error"] or "sha256" in result["error"]
    assert _hex_rom_dirs(roms_dir) == []


def test_truncated_assemble_validator_reject(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05)
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload("red.gb", len(rom), digest)
    server.append_gb_rom_upload(begun["upload_id"], 0, _b64(rom))
    result = server.finalize_gb_rom_upload(begun["upload_id"])
    assert result["accepted"] is False
    assert "1024" in result["error"]
    assert "1048576" in result["error"]
    assert _hex_rom_dirs(roms_dir) == []


def test_batch_upload_64kib_in_one_call(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom(rom_size_code=0x01)  # 64 KiB
    assert len(rom) == 64 * 1024
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload(
        "game.gb", len(rom), digest, email="owner@example.com"
    )
    assert begun["started"] is True
    chunk_size = begun["chunk_size"]
    assert chunk_size == 8 * 1024
    pieces = _slices(rom, chunk_size)
    assert len(pieces) == 8
    result = server.append_gb_rom_upload_batch(
        begun["upload_id"], 0, [_b64(piece) for piece in pieces]
    )
    assert result["appended"] is True
    assert result["upload_id"] == begun["upload_id"]
    assert result["chunks_accepted"] == 8
    assert result["received_bytes"] == len(rom)
    assert result["next_index"] == 8
    assert result["total_bytes"] == len(rom)

    finalized = server.finalize_gb_rom_upload(
        begun["upload_id"], email="owner@example.com"
    )
    assert finalized["accepted"] is True
    saved = config.ROOT / finalized["path"]
    assert saved.read_bytes() == rom


def test_oversized_batch_rejected(isolated_db, roms_dir: Path) -> None:
    digest = hashlib.sha256(b"unused").hexdigest()
    begun = server.begin_gb_rom_upload("game.gb", 128 * 1024, digest)
    assert begun["started"] is True
    chunk_size = begun["chunk_size"]
    assert chunk_size == 8 * 1024
    upload_id = begun["upload_id"]

    too_many_bytes = [_b64(b"\x00" * chunk_size) for _ in range(9)]
    oversized = server.append_gb_rom_upload_batch(upload_id, 0, too_many_bytes)
    assert oversized["appended"] is False
    assert "batch" in oversized["error"]
    meta = _upload_meta(roms_dir, upload_id)
    assert meta["received_bytes"] == 0
    assert meta["next_index"] == 0

    too_many_chunks = [_b64(b"\x00" * 1024) for _ in range(17)]
    counted = server.append_gb_rom_upload_batch(upload_id, 0, too_many_chunks)
    assert counted["appended"] is False
    assert "batch" in counted["error"]
    meta = _upload_meta(roms_dir, upload_id)
    assert meta["received_bytes"] == 0
    assert meta["next_index"] == 0


def test_batch_mid_hole_leaves_received_bytes_unchanged(
    isolated_db, roms_dir: Path
) -> None:
    rom = make_rom(rom_size_code=0x01)
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload("game.gb", len(rom), digest)
    chunk_size = begun["chunk_size"]
    upload_id = begun["upload_id"]
    first = rom[:chunk_size]
    second = rom[chunk_size : 2 * chunk_size]
    third = rom[2 * chunk_size : 3 * chunk_size]

    ok = server.append_gb_rom_upload(upload_id, 0, _b64(first))
    assert ok["appended"] is True
    assert ok["received_bytes"] == chunk_size

    hole = server.append_gb_rom_upload_batch(
        upload_id, 2, [_b64(second), _b64(third)]
    )
    assert hole["appended"] is False
    assert "chunk_index" in hole["error"]
    meta = _upload_meta(roms_dir, upload_id)
    assert meta["received_bytes"] == chunk_size
    assert meta["next_index"] == 1
    staged = (roms_dir / ".uploads" / upload_id / "data.bin").read_bytes()
    assert staged == first

    resumed = server.append_gb_rom_upload(upload_id, 1, _b64(second))
    assert resumed["appended"] is True
    assert resumed["received_bytes"] == 2 * chunk_size


def test_batch_mid_failure_rolls_back_received_bytes(isolated_db, roms_dir: Path) -> None:
    """A later chunk that would exceed total_bytes must not keep earlier batch writes."""
    chunk_size = 8 * 1024
    total = chunk_size + 1024
    digest = hashlib.sha256(b"unused").hexdigest()
    begun = server.begin_gb_rom_upload("game.gb", total, digest)
    upload_id = begun["upload_id"]
    first = b"\x11" * chunk_size
    overflow = b"\x22" * chunk_size
    result = server.append_gb_rom_upload_batch(
        upload_id, 0, [_b64(first), _b64(overflow)]
    )
    assert result["appended"] is False
    assert "received_bytes" in result["error"] or "exceed" in result["error"]
    meta = _upload_meta(roms_dir, upload_id)
    assert meta["received_bytes"] == 0
    assert meta["next_index"] == 0
    staged = (roms_dir / ".uploads" / upload_id / "data.bin").read_bytes()
    assert staged == b""


def test_oversized_chunk_rejected(isolated_db, roms_dir: Path) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload("game.gb", len(rom), digest)
    too_big = _b64(b"\x00" * (begun["chunk_size"] + 1))
    result = server.append_gb_rom_upload(begun["upload_id"], 0, too_big)
    assert result["appended"] is False
    assert "chunk" in result["error"]


def test_expired_upload_rejected(
    isolated_db, roms_dir: Path, monkeypatch
) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload("game.gb", len(rom), digest)
    monkeypatch.setattr(config, "ROM_UPLOAD_TTL_SECONDS", 0)
    result = server.append_gb_rom_upload(begun["upload_id"], 0, _b64(rom[:100]))
    assert result["appended"] is False
    assert "expired" in result["error"] or "unknown" in result["error"]


def test_submit_still_works_for_small_rom(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    rom = make_rom()
    result = server.submit_gb_rom(_b64(rom), filename="homebrew.gb")
    assert result["accepted"] is True
    saved = config.ROOT / result["path"]
    assert saved.read_bytes() == rom
    assert saved.name == "homebrew.gb"


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


def test_abort_gb_rom_upload_deletes_staging(isolated_db, roms_dir: Path) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = server.begin_gb_rom_upload("game.gb", len(rom), digest)
    upload_id = begun["upload_id"]
    dest = roms_dir / ".uploads" / upload_id
    assert dest.is_dir()
    result = server.abort_gb_rom_upload(upload_id)
    assert result["aborted"] is True
    assert result["upload_id"] == upload_id
    assert not dest.exists()
    again = server.abort_gb_rom_upload(upload_id)
    assert again["aborted"] is True
    assert _hex_rom_dirs(roms_dir) == []


def test_abort_gb_rom_upload_rejects_bad_id() -> None:
    result = server.abort_gb_rom_upload("not-an-id")
    assert result["aborted"] is False
    assert "upload_id" in result["error"]


def test_list_reclaims_expired_uploads(
    isolated_db, roms_dir: Path, monkeypatch
) -> None:
    rom = make_rom()
    digest = hashlib.sha256(rom).hexdigest()
    begun = upload_store.begin_upload(
        filename="game.gb", total_bytes=len(rom), sha256=digest
    )
    dest = roms_dir / ".uploads" / begun["upload_id"]
    assert dest.is_dir()
    monkeypatch.setattr(config, "ROM_UPLOAD_TTL_SECONDS", 0)
    listed = server.list_subdirectories_for_email("owner@example.com")
    assert listed["count"] == 0
    assert not dest.exists()


def test_finalize_replace_owned_truncated_mapping(
    fake_docker, isolated_db, roms_dir: Path, validator_module, pyboy_manager
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    name = _map_truncated(roms_dir)
    truncated = roms_dir / name / "Pokemon_-_Red_Version_USA_Europe_.gb"
    assert truncated.stat().st_size == 1024
    full = make_rom(title=b"POKEMON RED")  # 32 KiB playable dump
    upload_id = _chunked_upload(full, "red.gb", email="owner@example.com")
    result = server.finalize_gb_rom_upload(
        upload_id, email="owner@example.com", subdirectory=name
    )
    assert result["accepted"] is True
    assert result["saved"] is True
    assert result["mapped"] is True
    assert result["subdirectory"] == name
    hex_dirs = _hex_rom_dirs(roms_dir)
    assert [p.name for p in hex_dirs] == [name]
    chosen = _rom_in_subdirectory(name)
    assert chosen.read_bytes() == full
    assert not truncated.exists()
    leftovers = [
        p.name
        for p in (roms_dir / name).iterdir()
        if p.suffix.lower() in {".gb", ".gbc"}
    ]
    assert leftovers == [chosen.name]
    listed = server.list_subdirectories_for_email("owner@example.com")
    info = listed["subdirectories"][0]
    assert info["subdirectory"] == name
    game = info["games"][0]
    assert game["playable"] is True
    assert game["size_bytes"] == len(full)
    rom_file = next(e for e in info["files"] if e["filename"].endswith(".gb"))
    assert rom_file["playable"] is True
    assert rom_file["size_bytes"] == len(full)
    loaded = server.load_subdirectory_rom("owner@example.com", name)
    assert loaded["started"] is True
    assert loaded["running"] is True
    assert loaded["subdirectory"] == name


def test_finalize_subdirectory_not_owned_does_not_write(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    name = _map_truncated(roms_dir, email="owner@example.com")
    original = (roms_dir / name / "Pokemon_-_Red_Version_USA_Europe_.gb").read_bytes()
    full = make_rom()
    upload_id = _chunked_upload(full, "red.gb", email="intruder@example.com")
    result = server.finalize_gb_rom_upload(
        upload_id, email="intruder@example.com", subdirectory=name
    )
    assert result["accepted"] is False
    assert result["saved"] is False
    assert "not mapped" in result["error"]
    assert (roms_dir / name / "Pokemon_-_Red_Version_USA_Europe_.gb").read_bytes() == original
    assert _hex_rom_dirs(roms_dir) == [roms_dir / name]


def test_finalize_unmapped_subdirectory_does_not_write(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    name = "a" * db.SUBDIRECTORY_NAME_LENGTH
    (roms_dir / name).mkdir()
    marker = roms_dir / name / "keep.gb"
    marker.write_bytes(b"keep")
    full = make_rom()
    upload_id = _chunked_upload(full, "red.gb", email="owner@example.com")
    result = server.finalize_gb_rom_upload(
        upload_id, email="owner@example.com", subdirectory=name
    )
    assert result["accepted"] is False
    assert result["saved"] is False
    assert "not mapped" in result["error"]
    assert marker.read_bytes() == b"keep"
    assert [p.name for p in (roms_dir / name).iterdir()] == ["keep.gb"]


def test_finalize_invalid_subdirectory_does_not_persist(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    full = make_rom()
    upload_id = _chunked_upload(full, "red.gb", email="owner@example.com")
    result = server.finalize_gb_rom_upload(
        upload_id, email="owner@example.com", subdirectory="nope"
    )
    assert result["accepted"] is False
    assert result["saved"] is False
    assert "hexadecimal" in result["error"]
    assert _hex_rom_dirs(roms_dir) == []


def test_finalize_replace_requires_email(
    fake_docker, isolated_db, roms_dir: Path, validator_module
) -> None:
    fake_docker.setattr(
        server,
        "_validate_inside_container",
        lambda _cid, data: validator_module.validate_gb_rom_bytes(data),
    )
    name = _map_truncated(roms_dir)
    original = (roms_dir / name / "Pokemon_-_Red_Version_USA_Europe_.gb").read_bytes()
    full = make_rom()
    upload_id = _chunked_upload(full, "red.gb")
    result = server.finalize_gb_rom_upload(upload_id, subdirectory=name)
    assert result["accepted"] is False
    assert result["saved"] is False
    assert "email is required" in result["error"]
    assert (roms_dir / name / "Pokemon_-_Red_Version_USA_Europe_.gb").read_bytes() == original
