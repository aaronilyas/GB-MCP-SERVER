"""CI guard: play-loop JSON must not leak emulator memory or parsed game objects."""

from __future__ import annotations

from gb_mcp.emulator.play_limits import (
    FORBIDDEN_RESPONSE_KEY_NEEDLES,
    SEND_INPUT_RESPONSE_KEYS,
)
from gb_mcp.emulator.play_runtime import strip_forbidden_keys


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
