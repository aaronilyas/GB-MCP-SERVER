"""Docker-backed Game Boy play instances (`gb-pyboy-instance`).

The MCP process (host or `gb-mcp-server` container) talks to the Docker
daemon and starts one sibling container per ROM subdirectory. Communication
is `docker exec` to a loopback HTTP server inside the instance — instances
run with `--network=none`, no published ports, and no docker.sock.

Errors raised from this module are tool-safe: they never include docker
stdout/stderr dumps.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from gb_mcp import config
from gb_mcp.emulator.backend import (
    InstanceDeadError,
    InstanceHandle,
    _dead_message,
    play_container_name,
)
from gb_mcp.gb.header import assert_rom_playable
from gb_mcp.emulator.loop import (
    _state_path_for_rom,
    overlay_status,
    rewrite_host_email,
    shape_status,
)
from gb_mcp.emulator.play_limits import (
    DEFAULT_EMULATION_SPEED,
    INPUT_COMMAND_TIMEOUT_SECONDS,
)
from gb_mcp.isolation import docker as isolation


class DockerInstanceBackend:
    """Start / talk to / remove `gb-play-<subdir>` containers."""

    def start(
        self,
        email: str,
        subdirectory: str,
        rom_path: Path,
        *,
        idle_timeout_seconds: float,
        emulation_speed: int = DEFAULT_EMULATION_SPEED,
        restore_state: bool = True,
    ) -> InstanceHandle:
        try:
            assert_rom_playable(rom_path)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None

        name = play_container_name(subdirectory)
        if _container_running(name):
            return InstanceHandle(
                email=email,
                subdirectory=subdirectory,
                rom_path=rom_path,
                container_name=name,
                reused=True,
            )
        if _container_exists(name):
            _rm(name)
        _docker_available()
        _ensure_instance_image()
        args = play_create_args(
            subdirectory,
            rom_path,
            idle_timeout_seconds=idle_timeout_seconds,
            emulation_speed=emulation_speed,
            restore_state=restore_state,
        )
        created = isolation._run_docker(args, timeout=60)
        if created.returncode != 0:
            raise RuntimeError("Play instance failed to start")
        try:
            _wait_ready(name)
        except Exception:
            _rm(name)
            raise
        return InstanceHandle(
            email=email,
            subdirectory=subdirectory,
            rom_path=rom_path,
            container_name=name,
            reused=False,
        )

    def is_running(self, handle: InstanceHandle) -> bool:
        return _container_running(handle.container_name)

    def status(self, handle: InstanceHandle) -> dict[str, Any]:
        if not _container_running(handle.container_name):
            reason = _exit_close_reason(handle.container_name)
            payload = _host_status(handle, running=False, close_reason=reason)
            return payload
        remote = _rpc(handle.container_name, "GET", "/status", None, timeout=15)
        return _forwarded_rpc(handle, _overlay_host_status(handle, remote))

    def send_input(
        self,
        handle: InstanceHandle,
        steps: list[dict[str, Any]],
        screenshot_mode: str,
        *,
        timeout: float = INPUT_COMMAND_TIMEOUT_SECONDS,
        **extra: Any,
    ) -> dict[str, Any]:
        if not _container_running(handle.container_name):
            raise InstanceDeadError(_dead_message(_exit_close_reason(handle.container_name)))
        body = {"screenshot_mode": screenshot_mode, **extra}
        # Empty steps: [] is "steps provided" to parse_play_input and is
        # rejected; wait-only / hold / mash Docker calls must omit the key.
        if steps:
            body["steps"] = steps
        remote = _rpc(
            handle.container_name,
            "POST",
            "/input",
            body,
            timeout=int(timeout) + 5,
        )
        if remote.get("error") and not remote.get("pngs_b64") and not remote.get("pngs"):
            raise RuntimeError(str(remote["error"]))
        pngs_b64 = remote.pop("pngs_b64", [])
        pngs: list[bytes] = []
        for item in pngs_b64:
            try:
                pngs.append(base64.b64decode(item))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("Play instance returned an invalid screenshot") from exc
        remote["pngs"] = pngs
        gif_b64 = remote.pop("gif_b64", None)
        if isinstance(gif_b64, str) and gif_b64:
            try:
                remote["gif"] = base64.b64decode(gif_b64)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("Play instance returned an invalid screenshot") from exc
        else:
            remote.pop("gif", None)
        return _forwarded_rpc(handle, remote)

    def ping(self, handle: InstanceHandle, *, timeout: float = 10) -> dict[str, Any]:
        if not _container_running(handle.container_name):
            raise InstanceDeadError(_dead_message(_exit_close_reason(handle.container_name)))
        remote = _rpc(handle.container_name, "POST", "/ping", {}, timeout=int(timeout) + 5)
        if remote.get("error") and not remote.get("alive"):
            raise RuntimeError(str(remote["error"]))
        return _forwarded_rpc(handle, remote)

    def save(self, handle: InstanceHandle, *, timeout: float = 10) -> dict[str, Any]:
        if not _container_running(handle.container_name):
            raise InstanceDeadError(_dead_message(_exit_close_reason(handle.container_name)))
        remote = _rpc(handle.container_name, "POST", "/save", {}, timeout=int(timeout) + 5)
        if remote.get("error") and not remote.get("saved"):
            raise RuntimeError(str(remote["error"]))
        return _forwarded_rpc(handle, remote)

    def discard_state(self, handle: InstanceHandle, *, timeout: float = 10) -> dict[str, Any]:
        if not _container_running(handle.container_name):
            raise InstanceDeadError(_dead_message(_exit_close_reason(handle.container_name)))
        remote = _rpc(
            handle.container_name, "POST", "/discard_state", {}, timeout=int(timeout) + 5
        )
        if remote.get("error") and "discarded" not in remote:
            raise RuntimeError(str(remote["error"]))
        return _forwarded_rpc(handle, remote)

    def request_stop(self, handle: InstanceHandle, reason: str) -> None:
        if not _container_running(handle.container_name):
            return
        try:
            _rpc(
                handle.container_name,
                "POST",
                "/stop",
                {"reason": reason},
                timeout=20,
            )
        except InstanceDeadError:
            return
        except RuntimeError:
            # Stop must still proceed to join/rm so the volume save is kept.
            return

    def join(self, handle: InstanceHandle, timeout: float | None = 15) -> None:
        deadline = time.monotonic() + (15.0 if timeout is None else float(timeout))
        while time.monotonic() < deadline:
            if not _container_running(handle.container_name):
                _rm(handle.container_name)
                return
            time.sleep(0.1)
        _rm(handle.container_name)

    def reap(self, handle: InstanceHandle) -> None:
        _rm(handle.container_name)

    def close_reason(self, handle: InstanceHandle) -> str | None:
        if _container_running(handle.container_name):
            return None
        return _exit_close_reason(handle.container_name)


def play_create_args(
    subdirectory: str,
    rom_path: Path,
    *,
    idle_timeout_seconds: float,
    emulation_speed: int = DEFAULT_EMULATION_SPEED,
    restore_state: bool = True,
    image: str | None = None,
) -> list[str]:
    """Return `docker run` args for a locked-down play instance.

    Mounts only this subdirectory (ROM file read-only, directory read-write
    for the `.state` file). Does not mount docker.sock or other users' ROMs.
    """
    _require_subdirectory(subdirectory)
    host_rom = docker_bind_path(rom_path)
    host_subdir = docker_bind_path(rom_path.parent)
    rom_name = rom_path.name
    user = _run_as_user(rom_path)
    return [
        "run",
        "-d",
        "--name",
        play_container_name(subdirectory),
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
        user,
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--label",
        "gb-mcp.role=play",
        "--label",
        f"gb-mcp.subdirectory={subdirectory}",
        "--mount",
        f"type=bind,src={host_subdir},dst=/rom",
        "--mount",
        f"type=bind,src={host_rom},dst=/rom/{rom_name},readonly=true",
        "-e",
        f"GB_INSTANCE_ROM=/rom/{rom_name}",
        "-e",
        f"GB_INSTANCE_SUBDIRECTORY={subdirectory}",
        "-e",
        "GB_PYBOY_WINDOW=null",
        "-e",
        f"GB_PYBOY_IDLE_TIMEOUT_SECONDS={int(idle_timeout_seconds)}",
        "-e",
        f"GB_PYBOY_EMULATION_SPEED={int(emulation_speed)}",
        "-e",
        f"GB_INSTANCE_RESTORE_STATE={'1' if restore_state else '0'}",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-w",
        "/work",
        image or config.INSTANCE_IMAGE,
    ]


def docker_bind_path(path: Path) -> Path:
    """Translate a path under ROMS_DIR to the path the Docker daemon sees."""
    resolved = path.resolve()
    roms = config.ROMS_DIR.resolve()
    try:
        rel = resolved.relative_to(roms)
    except ValueError:
        return resolved
    return Path(config.roms_host_path()) / rel


def _require_subdirectory(name: str) -> str:
    if len(name) != 32 or any(c not in "0123456789abcdef" for c in name):
        raise ValueError("subdirectory must be a 32-character hexadecimal name")
    return name


def _run_as_user(rom_path: Path) -> str:
    try:
        st = rom_path.stat()
        return f"{st.st_uid}:{st.st_gid}"
    except OSError:
        return "10001:10001"


def _docker_available() -> None:
    try:
        isolation._docker_available()
    except RuntimeError:
        raise RuntimeError(
            "Docker is required and must be running to start a play instance."
        ) from None


def _ensure_instance_image() -> None:
    probe = isolation._run_docker(
        ["image", "inspect", config.INSTANCE_IMAGE],
        timeout=30,
    )
    if probe.returncode == 0:
        return
    dockerfile = config.ROOT / "Dockerfile.instance"
    if not dockerfile.is_file():
        raise RuntimeError(
            f"Play instance image {config.INSTANCE_IMAGE} is not present. "
            "Build it with compose or "
            "`docker build -t gb-pyboy-instance:latest -f Dockerfile.instance .`"
        )
    build = isolation._run_docker(
        [
            "build",
            "-t",
            config.INSTANCE_IMAGE,
            "-f",
            str(dockerfile),
            str(config.ROOT),
        ],
        timeout=600,
    )
    if build.returncode != 0:
        raise RuntimeError(f"Failed to build play instance image {config.INSTANCE_IMAGE}")


def _wait_ready(name: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_dead = False
    while time.monotonic() < deadline:
        if not _container_running(name):
            last_dead = True
            break
        try:
            payload = _rpc(name, "GET", "/health", None, timeout=5)
        except InstanceDeadError:
            last_dead = True
            break
        except RuntimeError:
            time.sleep(0.15)
            continue
        if payload.get("ready"):
            return
        time.sleep(0.15)
    if last_dead or not _container_running(name):
        raise InstanceDeadError(_ready_failure_message(name))
    raise RuntimeError("Play instance failed to start")


def _rpc(
    name: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    timeout: int,
) -> dict[str, Any]:
    payload = json.dumps(body).encode() if body is not None else b""
    result = isolation._run_docker(
        ["exec", "-i", name, "python", "/opt/instance/server.py", "rpc", method, path],
        input_bytes=payload,
        timeout=timeout,
    )
    if result.returncode != 0:
        if not _container_running(name):
            raise InstanceDeadError("Play instance is no longer running")
        raise RuntimeError("Play instance failed to handle the request")
    stdout = result.stdout.decode(errors="replace").strip()
    if not stdout:
        raise RuntimeError("Play instance returned an empty response")
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Play instance returned an invalid response") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Play instance returned an invalid response")
    return parsed


def _container_running(name: str) -> bool:
    result = isolation._run_docker(
        ["inspect", "-f", "{{.State.Running}}", name],
        timeout=15,
    )
    if result.returncode != 0:
        return False
    return result.stdout.decode().strip().lower() == "true"


def _container_exists(name: str) -> bool:
    result = isolation._run_docker(["inspect", name], timeout=15)
    return result.returncode == 0


def _container_exit_code(name: str) -> int | None:
    result = isolation._run_docker(
        ["inspect", "-f", "{{.State.ExitCode}}", name],
        timeout=15,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.decode().strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _exit_close_reason(name: str) -> str | None:
    code = _container_exit_code(name)
    if code is None:
        return None
    if code == 0:
        return "idle_timeout"
    if code == 2:
        return "requested"
    return "error"


def _extract_json_error(text: str) -> str | None:
    """Return a short reason from the last JSON ``{"error": ...}`` line.

    Ignores docker daemon dumps and stack traces. Tool responses must stay
    screenshot-only and must not include raw docker logs.
    """
    if not text:
        return None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if "Error response from daemon" in stripped:
            continue
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not payload.get("error"):
            continue
        error = str(payload["error"]).strip().splitlines()[0][:160]
        rom_bytes = payload.get("rom_bytes")
        expected = payload.get("expected_rom_bytes")
        if rom_bytes is not None and expected is not None:
            return f"{error} (rom_bytes={rom_bytes}, expected_rom_bytes={expected})"
        return error
    return None


def _boot_log_reason(name: str) -> str | None:
    result = isolation._run_docker(["logs", "--tail", "40", name], timeout=15)
    combined = (
        result.stderr.decode(errors="replace")
        + "\n"
        + result.stdout.decode(errors="replace")
    )
    if "Error response from daemon" in combined and "{" not in combined:
        return None
    return _extract_json_error(combined)


def _sanitized_boot_reason(name: str) -> str:
    parts: list[str] = []
    code = _container_exit_code(name)
    if code is not None:
        parts.append(f"exit {code}")
    log_reason = _boot_log_reason(name)
    if log_reason:
        parts.append(log_reason)
    text = "; ".join(parts)
    if "\n" in text or len(text) > 180:
        text = "; ".join(parts[:1] + ([log_reason[:120]] if log_reason else []))
        text = text.splitlines()[0][:180]
    return text


def _ready_failure_message(name: str) -> str:
    prefix = "Play instance exited before it became ready"
    detail = _sanitized_boot_reason(name)
    if not detail:
        return prefix
    return f"{prefix}: {detail}"


def _rm(name: str) -> None:
    try:
        isolation._run_docker(["rm", "-f", name], timeout=30)
    except Exception:
        pass


def _host_status(
    handle: InstanceHandle,
    *,
    running: bool,
    close_reason: str | None = None,
) -> dict[str, Any]:
    state = _state_path_for_rom(handle.rom_path)
    saved = False
    try:
        saved = state.is_file() and state.stat().st_size > 0
    except OSError:
        saved = False
    return shape_status(
        email=handle.email,
        subdirectory=handle.subdirectory,
        rom_path=handle.rom_path,
        running=running,
        saved=saved,
        close_reason=close_reason,
    )


def _overlay_host_status(handle: InstanceHandle, remote: dict[str, Any]) -> dict[str, Any]:
    return overlay_status(
        _host_status(handle, running=bool(remote.get("running", True))),
        remote,
    )


def _forwarded_rpc(handle: InstanceHandle, remote: dict[str, Any]) -> dict[str, Any]:
    """Replace instance-placeholder identity on a forwarded RPC body.

    Does not run ``overlay_status``: input replies include ``frames_advanced`` /
    ``pngs`` / classifiers that are not in ``REMOTE_STATUS_KEYS``.
    """
    if "subdirectory" in remote:
        remote["subdirectory"] = handle.subdirectory
    return rewrite_host_email(remote, handle.email)


# Imported by tests that assert bind-mount construction.
__all__ = [
    "DockerInstanceBackend",
    "docker_bind_path",
    "play_create_args",
    "play_container_name",
]
