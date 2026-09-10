"""Mash / long-hold actions return one GIF + one final PNG, never a PNG list."""

from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path

from PIL import Image as PILImage

from conftest import FakePyBoy
from gb_mcp.emulator.input_schema import parse_play_args, play_input_from_args
from gb_mcp.emulator.play_limits import (
    NATIVE_HEIGHT,
    NATIVE_WIDTH,
    PUBLIC_MASH_PRESS_FRAMES,
)
from gb_mcp.emulator.play_runtime import (
    execute_play_command,
    pack_action_media,
    strip_forbidden_keys,
    wants_action_gif,
)
from gb_mcp.tools.play import format_play_tool_result

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
    assert gif.startswith(b"GIF89a")
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


def test_instance_server_forwards_all_native_pngs() -> None:
    module = _instance_server()
    natives = [_png((i, 0, 80), size=(160, 144)) for i in range(3)]
    scaled = [_png((i, 0, 80), size=(640, 576)) for i in range(3)]
    encoded = module.encode_input_media(
        {"pngs": scaled, "pngs_native": natives, "sent": True}
    )
    assert "pngs" not in encoded
    assert "pngs_native" not in encoded
    assert len(encoded["pngs_b64"]) == 1
    assert len(encoded["pngs_native_b64"]) == 3
    decoded = [base64.b64decode(item) for item in encoded["pngs_native_b64"]]
    assert decoded == natives
    preview = PILImage.open(io.BytesIO(base64.b64decode(encoded["pngs_b64"][0])))
    assert preview.size == (640, 576)


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


def _dialogue_bar():
    import numpy as np

    frame = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 3), dtype=np.uint8)
    frame[:, :] = (80, 160, 80)
    frame[96:144, :, :] = (16, 16, 16)
    frame[100:140, 8:152, :] = (248, 248, 248)
    return frame


def _overworld_field():
    import numpy as np

    frame = np.zeros((NATIVE_HEIGHT, NATIVE_WIDTH, 3), dtype=np.uint8)
    light = (88, 168, 72)
    dark = (56, 136, 56)
    for y in range(0, NATIVE_HEIGHT, 8):
        for x in range(0, NATIVE_WIDTH, 8):
            color = light if ((x // 8) + (y // 8)) % 2 == 0 else dark
            frame[y : y + 8, x : x + 8] = color
    return frame


def _assert_gif_image(formatted) -> None:
    assert isinstance(formatted, list)
    assert len(formatted) == 2
    image = formatted[1]
    assert image._format == "gif"
    assert bytes(image.data).startswith(GIF_MAGIC)
    assert bytes(image.data).startswith(b"GIF89a")


def test_public_mash_returns_gif_without_media_video() -> None:
    from PIL import Image as PILImage

    pyboy = FakePyBoy(Path("dummy.gb"))
    box = _dialogue_bar()
    field = _overworld_field()

    def factory(ticks: int, _pressed: set[str]) -> PILImage.Image:
        if ticks < 24:
            return PILImage.fromarray(box)
        return PILImage.fromarray(field)

    pyboy.frame_factory = factory
    play = play_input_from_args(parse_play_args({"mash": True, "frames": 80}))
    assert play.macro == "mash"
    assert play.extra.get("media") == "image"
    result = execute_play_command(pyboy, play)
    gif = result.get("gif")
    assert isinstance(gif, (bytes, bytearray))
    assert bytes(gif).startswith(b"GIF89a")
    natives = result.get("pngs_native") or []
    assert len(natives) == 1
    formatted = format_play_tool_result(result, want_video=False)
    _assert_gif_image(formatted)
    status = formatted[0]
    assert len(status.get("screenshots") or []) <= 1


def test_public_long_hold_returns_gif_without_media_video() -> None:
    from dataclasses import replace

    pyboy = FakePyBoy(Path("dummy.gb"))
    play = play_input_from_args(parse_play_args({"buttons": ["up"], "frames": 40}))
    assert play.macro == "hold"
    assert play.extra.get("media") == "image"
    play = replace(play, disable_default_hold_abort=True, apply_default_hold_abort=False)
    result = execute_play_command(pyboy, play)
    gif = result.get("gif")
    assert isinstance(gif, (bytes, bytearray))
    assert bytes(gif).startswith(b"GIF89a")
    natives = result.get("pngs_native") or []
    assert len(natives) == 1
    formatted = format_play_tool_result(result, want_video=False)
    _assert_gif_image(formatted)


def test_public_short_tap_stays_png_only() -> None:
    pyboy = FakePyBoy(Path("dummy.gb"))
    play = play_input_from_args(parse_play_args({"buttons": ["a"], "frames": 16}))
    result = execute_play_command(pyboy, play)
    assert result.get("gif") is None
    formatted = format_play_tool_result(result, want_video=False)
    assert isinstance(formatted, list)
    assert formatted[1]._format == "png"
    assert bytes(formatted[1].data).startswith(PNG_MAGIC)


def test_public_mash_pulses_a_stops_when_box_gone_and_releases() -> None:
    from PIL import Image as PILImage

    pyboy = FakePyBoy(Path("dummy.gb"))
    box = _dialogue_bar()
    field = _overworld_field()

    def factory(ticks: int, _pressed: set[str]) -> PILImage.Image:
        if ticks < 28:
            return PILImage.fromarray(box)
        return PILImage.fromarray(field)

    pyboy.frame_factory = factory
    held_run = 0
    max_held = 0
    orig_tick = pyboy.tick

    def wrapped(count: int = 1, render: bool = True, sound: bool = True) -> bool:
        nonlocal held_run, max_held
        n = count if isinstance(count, int) and count > 0 else 0
        if "a" in pyboy._pressed:
            held_run += n
            if held_run > max_held:
                max_held = held_run
        else:
            held_run = 0
        return orig_tick(count, render, sound)

    pyboy.tick = wrapped  # type: ignore[method-assign]
    play = play_input_from_args(parse_play_args({"mash": True, "frames": 200}))
    result = execute_play_command(pyboy, play)
    assert result["frames_advanced"] < 200
    assert result["frames_advanced"] >= 28
    assert max_held <= PUBLIC_MASH_PRESS_FRAMES
    assert max_held < result["frames_advanced"]
    assert pyboy._pressed == set()
    assert pyboy.releases
    gif = result.get("gif")
    assert isinstance(gif, (bytes, bytearray))
    assert bytes(gif).startswith(GIF_MAGIC)
    formatted = format_play_tool_result(result)
    _assert_gif_image(formatted)


def test_public_mash_without_textbox_waits_briefly() -> None:
    from PIL import Image as PILImage

    pyboy = FakePyBoy(Path("dummy.gb"))
    field = _overworld_field()
    pyboy.frame_factory = lambda _ticks, _pressed: PILImage.fromarray(field)
    play = play_input_from_args(parse_play_args({"mash": True, "frames": 400}))
    result = execute_play_command(pyboy, play)
    assert result["frames_advanced"] <= 24
    assert pyboy._pressed == set()
    assert pyboy.presses.count("a") == 0
