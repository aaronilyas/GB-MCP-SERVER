from __future__ import annotations

import json
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from gb_mcp.isolation import docker


def test_validate_parses_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"valid": True, "reason": "ok"}

    def fake_run_docker(args, *, input_bytes=None, timeout=60):
        assert args[0] == "exec"
        assert input_bytes == b"rom"
        return CompletedProcess(args=args, returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")

    monkeypatch.setattr(docker, "_run_docker", fake_run_docker)
    assert docker._validate_inside_container("cid", b"rom") == payload


def test_validate_non_json_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        docker,
        "_run_docker",
        lambda *a, **k: CompletedProcess(args=[], returncode=0, stdout=b"not json", stderr=b""),
    )
    payload = docker._validate_inside_container("cid", b"rom")
    assert payload["valid"] is False
    assert "non-JSON" in payload["reason"]


def test_validate_nonzero_exit_without_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        docker,
        "_run_docker",
        lambda *a, **k: CompletedProcess(args=[], returncode=2, stdout=b"", stderr=b"boom"),
    )
    payload = docker._validate_inside_container("cid", b"rom")
    assert payload["valid"] is False
    assert payload["reason"] == "boom"


def test_destroy_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(docker, "_run_docker", boom)
    docker._destroy_container("cid")


def test_create_isolated_container_start_failure_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run_docker(args, *, input_bytes=None, timeout=60):
        calls.append(args)
        if args[0] == "create":
            return CompletedProcess(args=args, returncode=0, stdout=b"abc123\n", stderr=b"")
        if args[0] == "start":
            return CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"cannot start")
        return CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(docker, "_run_docker", fake_run_docker)
    with pytest.raises(RuntimeError, match="Failed to start isolated container"):
        docker._create_isolated_container()
    assert ["rm", "-f", "abc123"] in calls
    create_args = calls[0]
    assert "--network" in create_args and "none" in create_args
    assert "--cap-drop" in create_args and "ALL" in create_args


def test_docker_available_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Docker is required"):
        docker._docker_available()


def test_ensure_image_builds_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
        if cmd[:3] == ["docker", "build", "-t"]:
            return CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    docker._ensure_image()
    assert any(cmd[:2] == ["docker", "build"] for cmd in calls)


def test_ensure_image_requires_prebuilt_without_dockerfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from gb_mcp import config

    monkeypatch.setattr(config, "ROOT", tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="not present"):
        docker._ensure_image()
    assert not any(cmd[:2] == ["docker", "build"] for cmd in calls)


def test_play_create_args_lock_down_instance(roms_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    from gb_mcp import config
    from gb_mcp.emulator.instance import play_create_args

    monkeypatch.delenv("GB_ROMS_HOST_PATH", raising=False)
    name = "a" * 32
    dest = roms_dir / name
    dest.mkdir()
    rom = dest / "tetris.gb"
    rom.write_bytes(b"x")
    args = play_create_args(name, rom, idle_timeout_seconds=300)
    joined = " ".join(args)
    assert args[:2] == ["run", "-d"]
    assert "--network" in args and "none" in args
    assert "--cap-drop" in args and "ALL" in args
    assert "--memory" in args and "512m" in args
    assert "--cpus" in args
    assert "--pids-limit" in args
    assert "no-new-privileges:true" in args
    assert "--read-only" in args
    assert f"gb-play-{name}" in args
    assert "/var/run/docker.sock" not in joined
    assert "--privileged" not in args
    assert "readonly=true" in joined
    assert f"src={dest.resolve()},dst=/rom" in joined
    assert f"src={rom.resolve()},dst=/rom/tetris.gb,readonly=true" in joined
    # Bind this subdirectory only, never the whole roms/ tree.
    assert f"src={config.ROMS_DIR.resolve()},dst=" not in joined
    assert config.INSTANCE_IMAGE in args


def test_play_create_args_reject_bad_subdirectory(roms_dir) -> None:
    from gb_mcp.emulator.instance import play_create_args

    rom = roms_dir / "nope.gb"
    rom.write_bytes(b"x")
    with pytest.raises(ValueError, match="hexadecimal"):
        play_create_args("not-hex", rom, idle_timeout_seconds=30)


def test_docker_bind_path_uses_host_env(roms_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    from gb_mcp.emulator.instance import docker_bind_path

    monkeypatch.setenv("GB_ROMS_HOST_PATH", "/host/project/roms")
    name = "b" * 32
    rom = roms_dir / name / "g.gb"
    assert docker_bind_path(rom) == Path("/host/project/roms") / name / "g.gb"


def test_ensure_instance_image_builds_dockerfile_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gb_mcp import config
    from gb_mcp.emulator import instance as play
    from gb_mcp.isolation import docker as isolation

    calls: list[list[str]] = []

    def fake_run_docker(args, *, input_bytes=None, timeout=60):
        calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"")
        if args[:2] == ["build", "-t"]:
            return CompletedProcess(args=args, returncode=0, stdout=b"ok", stderr=b"")
        raise AssertionError(args)

    monkeypatch.setattr(isolation, "_run_docker", fake_run_docker)
    play._ensure_instance_image()
    build = next(args for args in calls if args[:2] == ["build", "-t"])
    assert config.INSTANCE_IMAGE in build
    assert "-f" in build
    assert any(str(arg).endswith("Dockerfile.instance") for arg in build)


def test_ensure_instance_image_requires_prebuilt_without_dockerfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from gb_mcp import config
    from gb_mcp.emulator import instance as play
    from gb_mcp.isolation import docker as isolation

    monkeypatch.setattr(config, "ROOT", tmp_path)
    calls: list[list[str]] = []

    def fake_run_docker(args, *, input_bytes=None, timeout=60):
        calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"")
        raise AssertionError(args)

    monkeypatch.setattr(isolation, "_run_docker", fake_run_docker)
    with pytest.raises(RuntimeError, match="not present"):
        play._ensure_instance_image()
    assert not any(args[:2] == ["build", "-t"] for args in calls)


def test_play_rpc_errors_are_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    from gb_mcp.emulator import instance as play
    from gb_mcp.emulator.backend import InstanceDeadError
    from gb_mcp.isolation import docker as isolation

    dump = b"Error response from daemon:\n" + b"X" * 4000

    def fake_run_docker(args, *, input_bytes=None, timeout=60):
        if args[0] == "inspect":
            return CompletedProcess(args=args, returncode=0, stdout=b"true\n", stderr=b"")
        return CompletedProcess(args=args, returncode=1, stdout=b"", stderr=dump)

    monkeypatch.setattr(isolation, "_run_docker", fake_run_docker)
    with pytest.raises(RuntimeError, match="failed to handle the request") as excinfo:
        play._rpc("gb-play-" + "a" * 32, "GET", "/health", None, timeout=5)
    assert "daemon" not in str(excinfo.value)
    assert "X" not in str(excinfo.value)

    def fake_dead(args, *, input_bytes=None, timeout=60):
        if args[0] == "inspect":
            return CompletedProcess(args=args, returncode=0, stdout=b"false\n", stderr=b"")
        return CompletedProcess(args=args, returncode=1, stdout=b"", stderr=dump)

    monkeypatch.setattr(isolation, "_run_docker", fake_dead)
    with pytest.raises(InstanceDeadError, match="no longer running") as dead:
        play._rpc("gb-play-" + "a" * 32, "POST", "/input", {"steps": []}, timeout=5)
    assert "daemon" not in str(dead.value)
    assert "X" not in str(dead.value)
