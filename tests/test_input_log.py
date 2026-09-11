"""Server-side JSONL input diary."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakePyBoy
from gb_mcp.emulator.input_schema import parse_play_args, parse_play_input, play_input_from_args
from gb_mcp.emulator.play_limits import DEFAULT_IDLE_TIMEOUT_SECONDS
from gb_mcp.emulator.play_runtime import execute_play_command, wants_action_gif


def test_input_log_appends_schema_line(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "input_log.jsonl"
    monkeypatch.setenv("GB_MCP_INPUT_LOG", str(log_path))
    pyboy = FakePyBoy(tmp_path / "dummy.gb")
    play = parse_play_input(
        {
            "macro": "hold",
            "buttons": ["up"],
            "hold_frames": 8,
            "disable_default_hold_abort": True,
        }
    )
    result = execute_play_command(pyboy, play)
    assert result.get("frames_advanced", 0) >= 1
    assert log_path.is_file()
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["t0"] == 0
    assert rec["t1"] >= rec["t0"]
    assert rec["buttons"] == ["up"]
    assert rec["hold_frames"] == 8
    assert rec["gap_frames"] == 0
    assert rec["macro"] == "hold"
    assert rec["intent"] is None
    assert "until" in rec
    assert "stopped_reason" in rec
    assert rec["frames_ran"] == result["frames_advanced"]
    assert rec["emulation_speed"] == 0


def test_idle_default_and_wants_gif_gate() -> None:
    assert DEFAULT_IDLE_TIMEOUT_SECONDS == 10800
    play = play_input_from_args(parse_play_args({"buttons": ["up"]}))
    assert wants_action_gif(play) is False
    video = play_input_from_args(
        parse_play_args({"buttons": ["up"], "media": "video"})
    )
    assert wants_action_gif(video) is True
