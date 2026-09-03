from __future__ import annotations

import json
import subprocess
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
