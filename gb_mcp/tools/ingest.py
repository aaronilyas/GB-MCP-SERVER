"""Small-homebrew ROM ingest. Large dumps use HTTP POST /roms, not MCP chunks."""

from __future__ import annotations

import base64
from typing import Any

import db
from gb_mcp import config
from gb_mcp.gb.header import assert_rom_playable
from gb_mcp.identity import require_email
from gb_mcp.isolation.docker import (
    _create_isolated_container,
    _destroy_container,
    _docker_available,
    _ensure_image,
    _validate_inside_container,
)
from gb_mcp.storage.roms import (
    _allocate_subdirectory_name,
    _persist_validated_rom,
    _sanitize_filename,
)
from gb_mcp.storage.uploads import delete_upload, take_assembled


def run_isolated_validation(rom_bytes: bytes) -> dict[str, Any]:
    """Container up first, then ROM bytes via stdin docker exec. Network none."""
    container_id: str | None = None
    try:
        _docker_available()
        _ensure_image()
        container_id = _create_isolated_container()
        return _validate_inside_container(container_id, rom_bytes)
    finally:
        if container_id:
            _destroy_container(container_id)


def _invalid_rom_result(validation: dict[str, Any], *, error: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "accepted": False,
        "saved": False,
        "id": None,
        "mapped": False,
        "error": error or validation.get("reason", "not a valid Game Boy ROM"),
    }


def _reject_unplayable_or_invalid(
    rom_bytes: bytes, validation: dict[str, Any]
) -> dict[str, Any] | None:
    if not validation.get("valid"):
        return _invalid_rom_result(validation)
    try:
        assert_rom_playable(rom_bytes)
    except ValueError as exc:
        rejected = dict(validation)
        rejected["valid"] = False
        rejected["reason"] = str(exc)
        rejected.pop("size_note", None)
        return _invalid_rom_result(rejected, error=str(exc))
    return None


def persist_mapped_rom(
    *,
    rom_bytes: bytes,
    filename: str,
    email: str,
    subdirectory: str | None = None,
) -> dict[str, Any]:
    """Persist a validated ROM and map it to ``email``. Host-internal."""
    safe_name = _sanitize_filename(filename)
    replace = subdirectory is not None
    if subdirectory is not None:
        try:
            with db.session_scope() as session:
                row = db.get_subdirectory_for_email(session, subdirectory, email)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "accepted": False,
                "saved": False,
                "id": subdirectory,
                "mapped": False,
                "error": str(exc),
            }
        if row is None:
            return {
                "ok": False,
                "accepted": False,
                "saved": False,
                "id": subdirectory,
                "mapped": False,
                "error": f"subdirectory {subdirectory!r} is not mapped to this email",
            }
    else:
        subdirectory = _allocate_subdirectory_name()
    dest = _persist_validated_rom(
        subdirectory, safe_name, rom_bytes, replace=replace
    )
    try:
        with db.session_scope() as session:
            mapped = db.map_subdirectory_to_email(session, subdirectory, email)
            email = mapped.user.email
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "accepted": True,
            "saved": True,
            "id": subdirectory,
            "mapped": False,
            "error": f"ROM saved but email could not be mapped: {exc}",
        }
    return {
        "ok": True,
        "accepted": True,
        "saved": True,
        "id": subdirectory,
        "mapped": True,
        "path": str(dest.relative_to(config.ROOT)),
    }


def add_rom(rom_base64: str, filename: str = "rom.gb") -> dict[str, Any]:
    """Validate a small homebrew ROM and map it from the current identity."""
    if len(rom_base64) > config.MAX_ROM_B64_CHARS:
        return {
            "ok": False,
            "accepted": False,
            "saved": False,
            "id": None,
            "mapped": False,
            "error": (
                f"ROM payload exceeds maximum encoded size of {config.MAX_ROM_B64_CHARS} "
                f"base64 characters ({config.MAX_ROM_BYTES} bytes decoded)"
            ),
        }
    try:
        rom_bytes = base64.b64decode(rom_base64, validate=True)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "accepted": False,
            "saved": False,
            "id": None,
            "mapped": False,
            "error": f"invalid base64 ROM payload: {exc}",
        }
    if not rom_bytes:
        return {
            "ok": False,
            "accepted": False,
            "saved": False,
            "id": None,
            "mapped": False,
            "error": "ROM payload is empty",
        }
    if len(rom_bytes) > config.MAX_ROM_BYTES:
        return {
            "ok": False,
            "accepted": False,
            "saved": False,
            "id": None,
            "mapped": False,
            "error": f"ROM exceeds maximum size of {config.MAX_ROM_BYTES} bytes",
        }

    bound = require_email()
    if isinstance(bound, dict):
        return bound

    try:
        validation = run_isolated_validation(rom_bytes)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "accepted": False,
            "saved": False,
            "id": None,
            "mapped": False,
            "error": str(exc),
        }

    rejected = _reject_unplayable_or_invalid(rom_bytes, validation)
    if rejected is not None:
        return rejected
    result = persist_mapped_rom(rom_bytes=rom_bytes, filename=filename, email=bound)
    result.pop("path", None)
    return result


def finalize_staged(
    upload_id: str,
    *,
    email: str,
    filename: str | None = None,
    subdirectory: str | None = None,
) -> dict[str, Any]:
    """Persist a completed storage upload. Host/HTTP internal, not an MCP tool."""
    try:
        rom_bytes, meta = take_assembled(upload_id)
    except ValueError as exc:
        return {
            "ok": False,
            "accepted": False,
            "saved": False,
            "id": None,
            "mapped": False,
            "error": str(exc),
        }
    try:
        validation = run_isolated_validation(rom_bytes)
        rejected = _reject_unplayable_or_invalid(rom_bytes, validation)
        if rejected is not None:
            return rejected
        chosen = filename or str(meta.get("filename") or "rom.gb")
        result = persist_mapped_rom(
            rom_bytes=rom_bytes,
            filename=chosen,
            email=email,
            subdirectory=subdirectory,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "accepted": False,
            "saved": False,
            "id": subdirectory,
            "mapped": False,
            "error": str(exc),
        }
    finally:
        delete_upload(upload_id)
