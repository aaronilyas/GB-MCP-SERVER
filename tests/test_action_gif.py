"""Mash / long-hold actions return one GIF + one final PNG, never a PNG list."""

from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path

from PIL import Image as PILImage

from conftest import FakePyBoy
from gb_mcp.emulator.play_runtime import (
    execute_play_command,
    pack_action_media,
    strip_forbidden_keys,
    wants_action_gif,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
GIF_MAGIC = b"GIF8"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _png(color: tuple[int, int, int], size: tuple[int, int] = (8, 8)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _instance_server():
    path = REPO_ROOT / "docker" / "instance_server.py"
    spec = importlib.util.spec_from_file_location("gb_instance_server_action_gif", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pack_action_media_skips_gif_for_one_png() -> None:
    png = _png((10, 20, 30))
    media = pack_action_media([png], want_gif=True)
    assert media["pngs"] == [png]
    assert "gif" not in media


def test_pack_action_media_encodes_one_gif_and_final_png() -> None:
    frames = [_png((i * 8, 40, 80)) for i in range(4)]
    media = pack_action_media(frames, want_gif=True)
    assert media["pngs"] == [frames[-1]]
    gif = media["gif"]
    assert gif.startswith(GIF_MAGIC)
    loaded = PILImage.open(io.BytesIO(gif))
    assert loaded.format == "GIF"
    n = 0
    try:
        while True:
            n += 1
            loaded.seek(loaded.tell() + 1)
    except EOFError:
        pass
    assert n >= 2
    duration = int(loaded.info.get("duration") or 0)
    assert 1000 <= duration * n <= 3000


def test_pack_action_media_without_gif_keeps_final_only() -> None:
    frames = [_png((1, 2, 3)), _png((4, 5, 6))]
    media = pack_action_media(frames, want_gif=False)
    assert media["pngs"] == [frames[-1]]
    assert "gif" not in media


def test_strip_keeps_gif_bytes() -> None:
    gif = b"GIF89a...."
    clean = strip_forbidden_keys({"sent": True, "gif": gif, "wram": 1})
    assert clean["gif"] == gif
    assert "wram" not in clean


def test_mash_execute_returns_one_png_and_gif() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    result = execute_play_command(
        pyboy,
        {
            "macro": "mash",
            "mash_button": "a",
            "max_frames": 40,
            "screenshot_scale": 1,
        },
    )
    pngs = result.get("pngs") or []
    assert len(pngs) == 1
    assert pngs[0].startswith(PNG_MAGIC)
    gif = result.get("gif")
    assert isinstance(gif, (bytes, bytearray))
    assert bytes(gif).startswith(GIF_MAGIC)
    assert result.get("screenshot_mode") == "final"
    assert result.get("screenshot_count") == 1
    assert result.get("macro") == "mash"


def test_long_hold_execute_returns_one_png_and_gif() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    result = execute_play_command(
        pyboy,
        {
            "macro": "hold",
            "buttons": ["up"],
            "max_frames": 40,
            "screenshot_scale": 1,
            "disable_default_hold_abort": True,
        },
    )
    pngs = result.get("pngs") or []
    assert len(pngs) == 1
    assert pngs[0].startswith(PNG_MAGIC)
    gif = result.get("gif")
    assert isinstance(gif, (bytes, bytearray))
    assert bytes(gif).startswith(GIF_MAGIC)
    assert result.get("screenshot_mode") == "final"


def test_short_tap_stays_png_only() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    result = execute_play_command(
        pyboy,
        {"buttons": ["a"], "hold_frames": 1, "screenshot_scale": 1},
    )
    pngs = result.get("pngs") or []
    assert len(pngs) == 1
    assert pngs[0].startswith(PNG_MAGIC)
    assert result.get("gif") is None
    assert result.get("screenshot_mode") == "final"
    assert wants_action_gif(type("P", (), {"macro": "buttons", "planned_frames": 1, "max_frames": 1})()) is False


def test_instance_server_encodes_at_most_one_png_and_gif() -> None:
    module = _instance_server()
    frames = [_png((i, 0, 80)) for i in range(30)]
    encoded = module.encode_input_media({"pngs": frames, "sent": True})
    assert "pngs" not in encoded
    assert len(encoded["pngs_b64"]) == 1
    assert len(encoded["pngs_b64"]) <= 1
    png = base64.b64decode(encoded["pngs_b64"][0])
    assert png.startswith(PNG_MAGIC)
    assert png == frames[-1]
    gif = base64.b64decode(encoded["gif_b64"])
    assert gif.startswith(GIF_MAGIC)


def test_instance_server_passes_through_existing_gif() -> None:
    module = _instance_server()
    png = _png((9, 9, 9))
    gif = pack_action_media([_png((1, 0, 0)), _png((0, 1, 0))], want_gif=True)["gif"]
    encoded = module.encode_input_media({"pngs": [png], "gif": gif})
    assert len(encoded["pngs_b64"]) == 1
    assert encoded["gif_b64"] == base64.b64encode(gif).decode("ascii")
    assert "gif" not in encoded
