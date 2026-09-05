"""Local warp repro script: three modes, no production changes, no ROM commits."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from conftest import FakePyBoy, REPO_ROOT


def _load_repro():
    path = REPO_ROOT / "scripts" / "repro_overworld_warp.py"
    spec = importlib.util.spec_from_file_location("repro_overworld_warp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rom_and_state(tmp_path: Path) -> tuple[Path, Path]:
    rom = tmp_path / "game.gb"
    rom.write_bytes(b"FAKEROM")
    state = Path(str(rom) + ".state")
    state.write_bytes(b"FAKESTATE")
    return rom, state


def test_help_does_not_need_a_rom() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "repro_overworld_warp.py"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Not an MCP tool" in result.stdout or "not an MCP tool" in result.stdout.lower()
    assert "--rom" in result.stdout
    assert "cold" in result.stdout
    assert "restore" in result.stdout
    assert "engine" in result.stdout


def test_cold_ignores_sibling_state(tmp_path: Path) -> None:
    repro = _load_repro()
    rom, _state = _rom_and_state(tmp_path)
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    out = tmp_path / "pngs"
    payload = repro.run_repro(
        rom=rom,
        mode="cold",
        direction="up",
        frames=8,
        out_dir=out,
        pyboy_factory=factory,
        screenshot_scale=1,
        call_timeout_seconds=5,
    )
    assert payload["frames_advanced"] == 8
    assert payload["restored_state"] is False
    assert created[0].loaded_state is None
    assert created[0].stopped is True
    before = out / "cold_up_before.png"
    after = out / "cold_up_after.png"
    assert before.is_file() and before.stat().st_size > 0
    assert after.is_file() and after.stat().st_size > 0


def test_restore_without_state_file_is_not_restored(tmp_path: Path) -> None:
    repro = _load_repro()
    rom = tmp_path / "game.gb"
    rom.write_bytes(b"FAKEROM")
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    payload = repro.run_repro(
        rom=rom,
        mode="restore",
        direction="down",
        frames=4,
        out_dir=tmp_path / "pngs",
        pyboy_factory=factory,
        screenshot_scale=1,
        call_timeout_seconds=5,
    )
    assert payload["restored_state"] is False
    assert created[0].loaded_state is None
    assert payload["frames_advanced"] == 4


def test_restore_loads_state_like_session_run(tmp_path: Path) -> None:
    repro = _load_repro()
    rom, state = _rom_and_state(tmp_path)
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    payload = repro.run_repro(
        rom=rom,
        mode="restore",
        direction="up",
        frames=8,
        out_dir=tmp_path / "pngs",
        pyboy_factory=factory,
        screenshot_scale=1,
        call_timeout_seconds=5,
    )
    assert payload["frames_advanced"] == 8
    assert payload["restored_state"] is True
    assert payload["state_path"] == str(state.resolve())
    assert created[0].loaded_state == b"FAKESTATE"
    assert False in created[0].tick_renders
    assert True in created[0].tick_renders
    assert "up" in created[0].presses
    assert created[0]._pressed == set()


def test_engine_drives_buttons_only_through_run_play_input(tmp_path: Path) -> None:
    repro = _load_repro()
    rom, _state = _rom_and_state(tmp_path)
    created: list[FakePyBoy] = []

    def factory(path: Path) -> FakePyBoy:
        instance = FakePyBoy(path)
        created.append(instance)
        return instance

    payload = repro.run_repro(
        rom=rom,
        mode="engine",
        direction="right",
        frames=8,
        out_dir=tmp_path / "pngs",
        pyboy_factory=factory,
        screenshot_scale=1,
        call_timeout_seconds=5,
    )
    assert payload["frames_advanced"] == 8
    assert payload["restored_state"] is True
    assert payload["macro"] == "hold"
    assert "right" in created[0].presses
    assert created[0].buttons == []
    assert created[0]._pressed == set()

    buf = io.StringIO()
    with redirect_stdout(buf):
        repro._print_result(payload)
    text = buf.getvalue()
    assert "frames_advanced=8" in text
    assert "restored_state=True" in text
