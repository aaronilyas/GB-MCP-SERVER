"""Resumable ROM ingest. Staging lives under roms/.uploads/, never roms/<32-hex>/."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from gb_mcp import config
from gb_mcp.gb.constants import MAX_ROM_BYTES
from gb_mcp.storage.roms import _sanitize_filename

_LOCK = threading.Lock()
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _uploads_root(*, create: bool = False) -> Path:
    root = config.rom_uploads_dir()
    if create:
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    return root


def _parse_sha256(value: str) -> str:
    digest = value.strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    return digest


def _parse_upload_id(upload_id: str) -> str:
    name = upload_id.strip().lower()
    if not _UPLOAD_ID_RE.fullmatch(name):
        raise ValueError("upload_id must be a 32-character hexadecimal id")
    return name


def _upload_dir(upload_id: str, *, create_root: bool = False) -> Path:
    name = _parse_upload_id(upload_id)
    root = _uploads_root(create=create_root).resolve()
    dest = (root / name).resolve()
    if not dest.is_relative_to(root):
        raise ValueError("upload_id must be a 32-character hexadecimal id")
    return dest


def _write_meta(dest: Path, meta: dict[str, Any]) -> None:
    payload = json.dumps(meta, separators=(",", ":")).encode()
    tmp = dest / ".meta.json.tmp"
    tmp.write_bytes(payload)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, dest / "meta.json")


def _read_meta(dest: Path) -> dict[str, Any]:
    raw = (dest / "meta.json").read_text(encoding="utf-8")
    meta = json.loads(raw)
    if not isinstance(meta, dict):
        raise ValueError("upload metadata is invalid")
    return meta


def expire_uploads(*, now: float | None = None) -> None:
    """Delete staging dirs older than ``ROM_UPLOAD_TTL_SECONDS`` (default 30 min)."""
    root = config.rom_uploads_dir()
    if not root.is_dir():
        return
    deadline = (time.time() if now is None else now) - config.ROM_UPLOAD_TTL_SECONDS
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        created = 0.0
        meta_path = child / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            created = float(meta.get("created_at") or 0)
        except Exception:
            created = 0.0
        if created <= deadline:
            shutil.rmtree(child, ignore_errors=True)


def _load_live(upload_id: str) -> tuple[Path, dict[str, Any]]:
    dest = _upload_dir(upload_id)
    if not dest.is_dir():
        raise ValueError("unknown or expired upload_id")
    try:
        meta = _read_meta(dest)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("unknown or expired upload_id") from exc
    created = float(meta.get("created_at") or 0)
    if time.time() - created > config.ROM_UPLOAD_TTL_SECONDS:
        shutil.rmtree(dest, ignore_errors=True)
        raise ValueError("unknown or expired upload_id")
    return dest, meta


def begin_upload(
    *,
    filename: str,
    total_bytes: int,
    sha256: str,
    email: str | None = None,
) -> dict[str, Any]:
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int):
        raise ValueError("total_bytes must be a positive integer")
    if total_bytes <= 0:
        raise ValueError("total_bytes must be a positive integer")
    if total_bytes > MAX_ROM_BYTES:
        raise ValueError(f"ROM exceeds maximum size of {MAX_ROM_BYTES} bytes")
    digest = _parse_sha256(sha256)
    safe_name = _sanitize_filename(filename)
    chunk_size = config.rom_upload_chunk_bytes()
    with _LOCK:
        expire_uploads()
        root = _uploads_root(create=True)
        upload_id = None
        dest = None
        for _ in range(8):
            candidate = uuid.uuid4().hex
            path = root / candidate
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                continue
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
            upload_id = candidate
            dest = path
            break
        if upload_id is None or dest is None:
            raise RuntimeError("failed to allocate a unique upload_id")
        meta = {
            "upload_id": upload_id,
            "filename": safe_name,
            "total_bytes": total_bytes,
            "sha256": digest,
            "email": email,
            "chunk_size": chunk_size,
            "next_index": 0,
            "received_bytes": 0,
            "created_at": time.time(),
        }
        data_path = dest / "data.bin"
        data_path.touch()
        try:
            os.chmod(data_path, 0o600)
        except OSError:
            pass
        _write_meta(dest, meta)
    return {
        "upload_id": upload_id,
        "chunk_size": chunk_size,
        "total_bytes": total_bytes,
        "filename": safe_name,
    }


def _validate_chunk_index(chunk_index: int, *, name: str = "chunk_index") -> int:
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
        raise ValueError(f"{name} must be an integer")
    if chunk_index < 0:
        raise ValueError(f"{name} must be an integer >= 0")
    return chunk_index


def _decode_chunk(chunk_base64: str, chunk_size: int) -> bytes:
    if not isinstance(chunk_base64, str) or not chunk_base64:
        raise ValueError("chunk_base64 is required")
    max_b64 = (chunk_size + 2) // 3 * 4 + 16
    if len(chunk_base64) > max_b64:
        raise ValueError("chunk exceeds the configured chunk_size")
    try:
        data = base64.b64decode(chunk_base64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid base64 chunk: {exc}") from None
    if not data:
        raise ValueError("chunk is empty")
    if len(data) > chunk_size:
        raise ValueError(
            f"chunk is {len(data)} bytes; maximum is {chunk_size} decoded bytes"
        )
    return data


def _apply_decoded_chunk(
    dest: Path,
    meta: dict[str, Any],
    *,
    chunk_index: int,
    data: bytes,
) -> dict[str, Any]:
    """Write one already-decoded chunk. Caller must hold ``_LOCK``."""
    expected_index = int(meta["next_index"])
    if chunk_index != expected_index:
        raise ValueError(
            f"expected chunk_index {expected_index}, got {chunk_index} "
            "(holes are not allowed)"
        )
    received = int(meta["received_bytes"]) + len(data)
    total = int(meta["total_bytes"])
    if received > total:
        raise ValueError(
            f"received_bytes {received} would exceed total_bytes {total}"
        )
    data_path = dest / "data.bin"
    with data_path.open("ab") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    meta["received_bytes"] = received
    meta["next_index"] = chunk_index + 1
    _write_meta(dest, meta)
    return {
        "received_bytes": received,
        "next_index": chunk_index + 1,
        "total_bytes": total,
        "upload_id": meta["upload_id"],
    }


def _rollback_upload(dest: Path, meta_before: dict[str, Any], size_before: int) -> None:
    data_path = dest / "data.bin"
    with data_path.open("r+b") as fh:
        fh.truncate(size_before)
        fh.flush()
        os.fsync(fh.fileno())
    _write_meta(dest, meta_before)


def append_chunk(
    *,
    upload_id: str,
    chunk_index: int,
    chunk_base64: str,
) -> dict[str, Any]:
    chunk_index = _validate_chunk_index(chunk_index)
    if not isinstance(chunk_base64, str) or not chunk_base64:
        raise ValueError("chunk_base64 is required")
    with _LOCK:
        expire_uploads()
        dest, meta = _load_live(upload_id)
        data = _decode_chunk(chunk_base64, int(meta["chunk_size"]))
        return _apply_decoded_chunk(
            dest, meta, chunk_index=chunk_index, data=data
        )


def append_chunks(
    *,
    upload_id: str,
    start_index: int,
    chunks_base64: list[str],
) -> dict[str, Any]:
    """Apply consecutive chunks in one call. Holds ``_LOCK`` for the whole batch."""
    start_index = _validate_chunk_index(start_index, name="start_index")
    if not isinstance(chunks_base64, list):
        raise ValueError("chunks_base64 must be a list of base64 strings")
    if not chunks_base64:
        raise ValueError("chunks_base64 must be a non-empty list")
    max_chunks = config.ROM_UPLOAD_BATCH_MAX_CHUNKS
    if len(chunks_base64) > max_chunks:
        raise ValueError(
            f"batch has {len(chunks_base64)} chunks; maximum is {max_chunks}"
        )
    for item in chunks_base64:
        if not isinstance(item, str) or not item:
            raise ValueError(
                "each chunk in chunks_base64 must be a non-empty base64 string"
            )

    with _LOCK:
        expire_uploads()
        dest, meta = _load_live(upload_id)
        chunk_size = int(meta["chunk_size"])
        max_batch_bytes = config.ROM_UPLOAD_BATCH_MAX_BYTES
        decoded: list[bytes] = []
        total_decoded = 0
        for item in chunks_base64:
            data = _decode_chunk(item, chunk_size)
            total_decoded += len(data)
            if total_decoded > max_batch_bytes:
                raise ValueError(
                    f"batch decoded size {total_decoded} exceeds maximum of "
                    f"{max_batch_bytes} bytes"
                )
            decoded.append(data)

        data_path = dest / "data.bin"
        size_before = data_path.stat().st_size
        meta_before = dict(meta)
        try:
            result = _apply_decoded_chunk(
                dest, meta, chunk_index=start_index, data=decoded[0]
            )
            for offset, data in enumerate(decoded[1:], start=1):
                result = _apply_decoded_chunk(
                    dest,
                    meta,
                    chunk_index=start_index + offset,
                    data=data,
                )
            result["chunks_accepted"] = len(decoded)
            return result
        except Exception:
            _rollback_upload(dest, meta_before, size_before)
            raise


def take_assembled(upload_id: str) -> tuple[bytes, dict[str, Any]]:
    """Return assembled bytes + meta. Caller must ``delete_upload`` in finally."""
    with _LOCK:
        expire_uploads()
        dest, meta = _load_live(upload_id)
        data_path = dest / "data.bin"
        try:
            data = data_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"could not read assembled ROM: {exc}") from exc
        total = int(meta["total_bytes"])
        if len(data) != total:
            raise ValueError(
                f"upload incomplete: received {len(data)} of {total} bytes"
            )
        digest = hashlib.sha256(data).hexdigest()
        expected = str(meta["sha256"])
        if not hmac.compare_digest(digest, expected):
            raise ValueError("sha256 does not match the assembled ROM")
        return data, meta


def delete_upload(upload_id: str) -> None:
    try:
        dest = _upload_dir(upload_id)
    except ValueError:
        return
    with _LOCK:
        shutil.rmtree(dest, ignore_errors=True)


def abort_upload(upload_id: str) -> dict[str, Any]:
    """Delete staging for ``upload_id``. Invalid ids raise; missing dirs succeed."""
    dest = _upload_dir(upload_id)
    name = dest.name
    with _LOCK:
        shutil.rmtree(dest, ignore_errors=True)
    return {"upload_id": name}
