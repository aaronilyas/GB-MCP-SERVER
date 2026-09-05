from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from gb_mcp.emulator.backend import InstanceDeadError, InstanceHandle
from gb_mcp.emulator.instance import DockerInstanceBackend, _wait_ready
from gb_mcp.isolation import docker as isolation

from rom_builder import make_rom


def test_truncated_rom_never_calls_docker_run(
    roms_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run_docker(args, *, input_bytes=None, timeout=60):  # noqa: ARG001
        calls.append(args)
        raise AssertionError("docker must not be invoked for a truncated ROM")

    monkeypatch.setattr(isolation, "_run_docker", fake_run_docker)
    name = "a" * 32
    dest = roms_dir / name
    dest.mkdir()
    rom = dest / "red.gb"
    rom.write_bytes(make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05))
    backend = DockerInstanceBackend()
    with pytest.raises(RuntimeError, match="truncated") as excinfo:
        backend.start("owner@example.com", name, rom, idle_timeout_seconds=30)
    message = str(excinfo.value)
    assert "1024" in message
    assert "1048576" in message
    assert "0x05" in message
    assert calls == []


def test_wait_ready_includes_sanitized_boot_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump = b"Error response from daemon:\n" + b"X" * 4000
    payload = json.dumps(
        {
            "error": "PyBoy failed to boot: invalid cartridge",
            "rom_bytes": 1024,
            "expected_rom_bytes": 1048576,
        }
    ).encode()

    def fake_run_docker(args, *, input_bytes=None, timeout=60):  # noqa: ARG001
        if args[0] == "inspect":
            return CompletedProcess(args=args, returncode=0, stdout=b"false\n", stderr=b"")
        if args[0] == "logs":
            return CompletedProcess(
                args=args,
                returncode=0,
                stdout=dump,
                stderr=payload + b"\n",
            )
        raise AssertionError(args)

    monkeypatch.setattr(isolation, "_run_docker", fake_run_docker)
    with pytest.raises(InstanceDeadError) as excinfo:
        _wait_ready("gb-play-" + "a" * 32, timeout=0.2)
    message = str(excinfo.value)
    assert message.startswith("Play instance exited before it became ready")
    assert message != "Play instance exited before it became ready"
    assert "PyBoy failed to boot" in message
    assert "1024" in message
    assert "1048576" in message
    assert "daemon" not in message
    assert "X" not in message
    assert "\n" not in message


def test_wait_ready_strips_docker_log_blobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump = b"Error response from daemon:\n" + b"SECRET_DOCKER_DUMP\n" * 80

    def fake_run_docker(args, *, input_bytes=None, timeout=60):  # noqa: ARG001
        if args[0] == "inspect":
            return CompletedProcess(args=args, returncode=0, stdout=b"false\n", stderr=b"")
        if args[0] == "logs":
            return CompletedProcess(args=args, returncode=0, stdout=dump, stderr=b"")
        return CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(isolation, "_run_docker", fake_run_docker)
    with pytest.raises(InstanceDeadError) as excinfo:
        _wait_ready("gb-play-" + "b" * 32, timeout=0.2)
    message = str(excinfo.value)
    assert "SECRET_DOCKER_DUMP" not in message
    assert "daemon" not in message.lower()
    assert "Play instance exited before it became ready" in message


def test_send_input_omits_empty_steps_for_wait_only_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wait-only Docker RPCs must not send steps:[]; parse_play_input rejects that."""
    captured: list[dict] = []

    def fake_rpc(name, method, path, body, *, timeout):  # noqa: ARG001
        captured.append(body)
        return {"pngs_b64": []}

    monkeypatch.setattr(
        "gb_mcp.emulator.instance._container_running", lambda _name: True
    )
    monkeypatch.setattr("gb_mcp.emulator.instance._rpc", fake_rpc)

    backend = DockerInstanceBackend()
    handle = InstanceHandle(
        email="owner@example.com",
        subdirectory="a" * 32,
        rom_path=Path("rom.gb"),
        container_name="gb-play-" + "a" * 32,
    )
    backend.send_input(handle, [], "final", wait=True, hold_frames=8)
    assert captured[0]["wait"] is True
    assert captured[0]["hold_frames"] == 8
    assert captured[0]["screenshot_mode"] == "final"
    assert "steps" not in captured[0]

    wait_steps = [{"buttons": [], "hold_frames": 8, "wait": True}]
    backend.send_input(handle, wait_steps, "final")
    assert captured[1]["steps"] == wait_steps


def test_instance_server_truncated_rom_writes_json_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rom = tmp_path / "red.gb"
    rom.write_bytes(make_rom(size=1024, title=b"POKEMON RED", rom_size_code=0x05))
    monkeypatch.setenv("GB_INSTANCE_ROM", str(rom))
    monkeypatch.setenv("GB_INSTANCE_SUBDIRECTORY", "a" * 32)
    path = Path(__file__).resolve().parents[1] / "docker" / "instance_server.py"
    spec = importlib.util.spec_from_file_location("gb_instance_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    code = module.main([])
    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert "truncated" in payload["error"]
    assert payload["rom_bytes"] == 1024
    assert payload["expected_rom_bytes"] == 1048576


def test_instance_server_main_joins_without_timeout() -> None:
    """A 15s join() return would kill the daemon PyBoy thread at boot."""
    path = Path(__file__).resolve().parents[1] / "docker" / "instance_server.py"
    source = path.read_text(encoding="utf-8")
    assert "session.join(timeout=None)" in source
