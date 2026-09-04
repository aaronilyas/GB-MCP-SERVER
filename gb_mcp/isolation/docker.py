"""Run Game Boy ROM validation inside a locked-down Docker container."""

from __future__ import annotations

import json
import subprocess
import uuid
from typing import Any

from gb_mcp import config


def _docker_available() -> None:
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Docker is required and must be running to validate ROMs in isolation."
        ) from exc


def _ensure_image() -> None:
    probe = subprocess.run(
        ["docker", "image", "inspect", config.DOCKER_IMAGE],
        capture_output=True,
        timeout=30,
    )
    if probe.returncode == 0:
        return
    dockerfile = config.ROOT / "Dockerfile"
    if not dockerfile.is_file():
        raise RuntimeError(
            f"Docker image {config.DOCKER_IMAGE} is not present. "
            "Build it with compose or `docker build -t gb-rom-validator:latest .`"
        )
    build = subprocess.run(
        ["docker", "build", "-t", config.DOCKER_IMAGE, str(config.ROOT)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if build.returncode != 0:
        raise RuntimeError(
            f"Failed to build Docker image {config.DOCKER_IMAGE}:\n"
            f"{build.stderr or build.stdout}"
        )


def _run_docker(args: list[str], *, input_bytes: bytes | None = None, timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", *args],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )


def _create_isolated_container() -> str:
    """Start a locked-down container before any ROM bytes are loaded into it."""
    name = f"gb-rom-validate-{uuid.uuid4().hex[:12]}"
    create = _run_docker(
        [
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--tmpfs",
            "/work:rw,noexec,nosuid,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            config.DOCKER_IMAGE,
            "sleep",
            "infinity",
        ],
        timeout=60,
    )
    if create.returncode != 0:
        raise RuntimeError(
            f"Failed to create isolated container: {create.stderr.decode(errors='replace')}"
        )
    container_id = create.stdout.decode().strip()
    start = _run_docker(["start", container_id], timeout=60)
    if start.returncode != 0:
        _run_docker(["rm", "-f", container_id], timeout=30)
        raise RuntimeError(
            f"Failed to start isolated container: {start.stderr.decode(errors='replace')}"
        )
    return container_id


def _validate_inside_container(container_id: str, rom_bytes: bytes) -> dict[str, Any]:
    """Load ROM bytes into the already-running container via stdin and validate.

    The ROM is not written on the host before the isolated container exists; it is
    streamed into `docker exec` only after the container is up with --network=none.
    """
    result = _run_docker(
        [
            "exec",
            "-i",
            container_id,
            "python3",
            "/opt/validator/validate_gb_rom.py",
            "-",
        ],
        input_bytes=rom_bytes,
        timeout=60,
    )
    stdout = result.stdout.decode(errors="replace").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        payload = {
            "valid": False,
            "reason": f"validator returned non-JSON output: {stdout!r}",
        }
    if result.returncode != 0 and "valid" not in payload:
        payload = {
            "valid": False,
            "reason": (
                stdout
                or result.stderr.decode(errors="replace")
                or f"validator exited {result.returncode}"
            ),
        }
    return payload


def _destroy_container(container_id: str) -> None:
    try:
        _run_docker(["rm", "-f", container_id], timeout=30)
    except Exception:
        # Teardown must not mask validation results or crash the MCP tool.
        pass
