"""CI guard: play-loop JSON must not leak emulator memory or parsed game objects."""

from __future__ import annotations

from pathlib import Path

from gb_mcp.emulator.loop import PUBLIC_STATUS_KEYS, shape_public_status, shape_status
from gb_mcp.emulator.play_limits import (
    FORBIDDEN_RESPONSE_KEY_NEEDLES,
    SEND_INPUT_RESPONSE_KEYS,
)
from gb_mcp.emulator.play_runtime import strip_forbidden_keys

PUBLIC_STRIPPED_KEYS = (
    "region_hashes",
    "rom_path",
    "idle_timeout_seconds",
    "seconds_until_idle_close",
    "seconds_since_last_input",
    "native_size",
    "email",
    "subdirectory",
    "ocr_text",
    "ocr_engine",
    "ocr_error",
    "classifiers",
    "screenshot_mode",
    "hash",
    "blake2s",
    "battle_likely",
    "textbox_likely",
    "start_menu_likely",
    "window_occluded_likely",
    "pngs",
)
CLASSIFIER_NAME_NEEDLES = (
    "battle_likely",
    "textbox_likely",
    "start_menu_likely",
    "window_occluded_likely",
    "classifiers",
)


def _flatten_keys(payload: object, prefix: str = "") -> list[str]:
    keys: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.append(path)
            keys.extend(_flatten_keys(value, path))
    elif isinstance(payload, list):
        for item in payload[:12]:
            keys.extend(_flatten_keys(item, prefix))
    return keys


def test_forbidden_needles_are_blocked_on_sample_payload() -> None:
    payload = {
        "sent": True,
        "stop_reason": "completed",
        "frames_advanced": 8,
        "emulation_speed": 0,
        "until_fired": False,
        "region_hashes": {"full": "aa", "bottom": "bb", "center": "cc"},
        "classifiers": {
            "textbox_likely": False,
            "battle_likely": False,
            "start_menu_likely": False,
        },
        "screenshot_scale": 4,
        "native_size": [160, 144],
        "screenshot_mode": "final",
        "screenshot_count": 1,
        "screenshots": [{"kind": "final", "frame_index": 8, "step_index": 0}],
        "macro": "buttons",
        "email": "owner@example.com",
        "subdirectory": "a" * 32,
        "rom": "game.gb",
        "running": True,
        "saved": False,
    }
    for path in _flatten_keys(payload):
        joined = path.lower()
        for needle in FORBIDDEN_RESPONSE_KEY_NEEDLES:
            assert needle not in joined, (path, needle)


def test_ocr_missing_engine_does_not_raise() -> None:
    from gb_mcp.emulator.ocr import ocr_pngs
    from gb_mcp.emulator.play_runtime import _maybe_ocr

    result = ocr_pngs([])
    assert "ocr_text" in result
    skipped = _maybe_ocr([])
    assert skipped.get("ocr_error") in {None, "disabled"} or skipped.get("ocr_text") is not None


def test_strip_drops_memory_keys() -> None:
    dirty = {
        "sent": True,
        "wram": b"nope",
        "memory": {"player_x": 1},
        "frames_advanced": 1,
        "stop_reason": "completed",
    }
    clean = strip_forbidden_keys(dirty)
    assert "wram" not in clean
    assert "memory" not in clean
    assert clean["frames_advanced"] == 1
    assert set(clean) <= SEND_INPUT_RESPONSE_KEYS


def _dirty_internal_status() -> dict:
    return {
        "sent": True,
        "ok": True,
        "stop_reason": "completed",
        "frames_advanced": 8,
        "emulation_speed": 0,
        "until_fired": False,
        "region_hashes": {"full": "aa", "bottom": "bb", "center": "cc"},
        "classifiers": {
            "textbox_likely": True,
            "battle_likely": True,
            "start_menu_likely": True,
            "window_occluded_likely": True,
        },
        "screenshot_scale": 4,
        "native_size": [160, 144],
        "screenshot_mode": "final",
        "screenshot_count": 1,
        "screenshots": [{"kind": "final", "frame_index": 8, "step_index": 0}],
        "pngs": [b"\x89PNG"],
        "macro": "buttons",
        "email": "owner@example.com",
        "subdirectory": "a" * 32,
        "rom": "game.gb",
        "rom_path": "roms/" + "a" * 32 + "/game.gb",
        "running": True,
        "saved": False,
        "cartridge_title": "POKEMON RED",
        "idle_timeout_seconds": 300,
        "seconds_until_idle_close": 12.5,
        "seconds_since_last_input": 1.25,
        "ocr_text": "HELLO",
        "ocr_engine": "tesseract",
        "ocr_error": None,
        "hash": "deadbeef",
        "blake2s": "cafebabe",
        "battle_likely": True,
        "textbox_likely": True,
        "start_menu_likely": True,
        "window_occluded_likely": True,
    }


def test_shape_public_status_drops_internal_fields() -> None:
    public = shape_public_status(_dirty_internal_status())
    for key in PUBLIC_STRIPPED_KEYS:
        assert key not in public
    assert set(public) <= PUBLIC_STATUS_KEYS
    assert public["ok"] is True
    assert public["frames"] == 8
    assert public["stopped"] is False
    assert public["game"] == "POKEMON RED"
    assert "/" not in (public["game"] or "")
    assert public["looks_like"] == "battle"
    classifier_fields = [
        path
        for path in _flatten_keys(public)
        if any(needle in path.lower() for needle in CLASSIFIER_NAME_NEEDLES)
    ]
    assert classifier_fields == []
    assert "looks_like" in public
    for path in _flatten_keys(public):
        joined = path.lower()
        for needle in FORBIDDEN_RESPONSE_KEY_NEEDLES:
            assert needle not in joined, (path, needle)
        for leak in ("hash", "blake2s", "rom_path", "ocr_", "email", "png"):
            assert leak not in joined, (path, leak)


def test_shape_public_status_allowlist_and_error() -> None:
    public = shape_public_status(
        {
            "error": "session exploded",
            "running": False,
            "rom_path": "/secret/roms/aabb/tetris.gb",
            "region_hashes": {"full": "aa"},
            "classifiers": {"battle_likely": False},
        }
    )
    assert public == {
        "ok": False,
        "frames": 0,
        "stopped": True,
        "game": "tetris",
        "error": "session exploded",
    }
    assert set(public) <= PUBLIC_STATUS_KEYS
    assert "looks_like" not in public
    assert "/" not in (public["game"] or "")


def test_shape_public_status_looks_like_priority_and_omit() -> None:
    assert (
        shape_public_status(
            {"classifiers": {"textbox_likely": True, "start_menu_likely": True}}
        )["looks_like"]
        == "textbox"
    )
    assert (
        shape_public_status(
            {"classifiers": {"start_menu_likely": True, "window_occluded_likely": True}}
        )["looks_like"]
        == "menu"
    )
    public_fade = shape_public_status({"classifiers": {"window_occluded_likely": True}})
    assert public_fade["looks_like"] == "fade"
    assert "window_occluded_likely" not in public_fade
    assert "classifiers" not in public_fade
    assert "looks_like" not in shape_public_status(
        {"classifiers": {"battle_likely": False, "textbox_likely": False}}
    )


def test_shape_status_keeps_internal_engine_fields() -> None:
    rom_path = Path("game.gb")
    payload = shape_status(
        email="owner@example.com",
        subdirectory="a" * 32,
        rom_path=rom_path,
        running=True,
        frames_advanced=3,
        region_hashes={"full": "aa"},
        classifiers={"battle_likely": True},
        idle_timeout_seconds=300,
    )
    assert payload["email"] == "owner@example.com"
    assert payload["subdirectory"] == "a" * 32
    assert payload["rom_path"]
    assert payload["region_hashes"]["full"] == "aa"
    assert payload["classifiers"]["battle_likely"] is True
    assert payload["frames_advanced"] == 3
    public = shape_public_status(payload)
    assert "email" not in public
    assert "region_hashes" not in public
    assert public["looks_like"] == "battle"
    assert public["frames"] == 3
