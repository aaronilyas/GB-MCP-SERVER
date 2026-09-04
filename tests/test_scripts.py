"""Operator start/stop scripts wrap Compose and sibling play containers."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["start.sh", "stop.sh"])
def test_script_is_executable_bash(name: str) -> None:
    path = ROOT / name
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert path.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(path)], check=True, capture_output=True)


@pytest.mark.parametrize("name", ["start.sh", "stop.sh"])
def test_script_help_does_not_need_docker(name: str) -> None:
    result = subprocess.run(
        ["bash", str(ROOT / name), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Usage:" in result.stdout
    assert result.stderr == ""


def test_start_bootstraps_sqlite_and_uses_compose() -> None:
    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "user_subdirectories.sqlite3" in text
    assert "docker compose" in text
    assert "--profile" in text
    assert "GB_MCP_BEARER_TOKEN" in text
    assert "GB_MCP_JWT_SECRET" in text
    assert "TUNNEL_TOKEN" in text


def test_stop_saves_sibling_play_instances() -> None:
    text = (ROOT / "stop.sh").read_text(encoding="utf-8")
    assert "gb-mcp.role=play" in text
    assert "rpc POST /stop" in text
    assert "gb-rom-validate-" in text
    assert "--profile" in text
    assert "tunnel" in text


@pytest.mark.parametrize("name", ["start.sh", "stop.sh"])
def test_script_rejects_unknown_arguments(name: str) -> None:
    result = subprocess.run(
        ["bash", str(ROOT / name), "--not-a-flag"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "unknown argument" in result.stderr
    assert "Usage:" in result.stderr
