#!/usr/bin/env python3
"""Headless PyBoy control plane for the `gb-pyboy-instance` image.

Listens on 127.0.0.1 only. The MCP host reaches this process with
`docker exec` — instances run `--network=none` with no published ports.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from gb_mcp.emulator.loop import (
    EmulatorSession,
    _default_pyboy_factory,
)
from gb_mcp.emulator.play_limits import (
    DEFAULT_EMULATION_SPEED,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    INPUT_COMMAND_TIMEOUT_SECONDS,
)
from gb_mcp.gb.header import inspect_rom_playable

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GB_INSTANCE_PORT", "8080"))
RPC_URL = f"http://{LISTEN_HOST}:{LISTEN_PORT}"


class _State:
    session: EmulatorSession | None = None
    ready = False
    httpd: ThreadingHTTPServer | None = None


STATE = _State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            session = STATE.session
            self._send_json(
                {
                    "ready": bool(STATE.ready and session is not None and session.is_running),
                }
            )
            return
        if self.path == "/status":
            session = STATE.session
            if session is None:
                self._send_json({"running": False, "error": "Play instance is no longer running"}, 503)
                return
            self._send_json(session.status())
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, 400)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "invalid JSON"}, 400)
            return
        session = STATE.session
        if session is None or not session.is_running:
            self._send_json({"error": "Play instance is no longer running"}, 503)
            return
        if self.path == "/input":
            payload = dict(body)
            if not payload.get("steps"):
                payload.pop("steps", None)
            if not payload.get("screenshot_mode"):
                payload["screenshot_mode"] = "final"
            try:
                result = session.submit(
                    "input",
                    timeout=INPUT_COMMAND_TIMEOUT_SECONDS,
                    **payload,
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json({"sent": False, "error": str(exc)}, 400)
                return
            pngs = result.pop("pngs", [])
            result["pngs_b64"] = [base64.b64encode(png).decode("ascii") for png in pngs]
            self._send_json(result)
            return
        if self.path == "/ping":
            try:
                result = session.submit("ping", timeout=10)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"alive": False, "error": str(exc)}, 400)
                return
            self._send_json(session.status(**result))
            return
        if self.path == "/save":
            try:
                result = session.submit("save", timeout=10)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"saved": False, "error": str(exc)}, 400)
                return
            self._send_json(session.status(**result))
            return
        if self.path == "/stop":
            reason = str(body.get("reason") or "requested")
            session.request_stop(reason)
            session.join(timeout=15)
            self._send_json(session.status(stopped=True))
            return
        self._send_json({"error": "not found"}, 404)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _rpc_cli(method: str, path: str) -> int:
    body = sys.stdin.buffer.read() if method.upper() != "GET" else b""
    req = urllib.request.Request(
        f"{RPC_URL}{path}",
        data=body or None,
        method=method.upper(),
    )
    if body:
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=INPUT_COMMAND_TIMEOUT_SECONDS + 10) as resp:
            sys.stdout.buffer.write(resp.read())
            return 0
    except urllib.error.HTTPError as exc:
        sys.stdout.buffer.write(exc.read())
        return 0
    except Exception:
        return 2


def _exit_code(reason: str | None) -> int:
    if reason in {"requested", "switched", "shutdown"}:
        return 2
    if reason == "error":
        return 1
    return 0


def _short_exception(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:160]


def _boot_failure_payload(rom: Path, exc: BaseException | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        info = inspect_rom_playable(rom)
        payload["rom_bytes"] = info.get("size_bytes")
        payload["expected_rom_bytes"] = info.get("expected_rom_bytes")
        if not info.get("playable") and info.get("unplayable_reason"):
            payload["error"] = info["unplayable_reason"]
            return payload
    except Exception:
        pass
    if exc is not None:
        payload["error"] = f"PyBoy failed to boot: {_short_exception(exc)}"
    else:
        payload["error"] = "PyBoy failed to boot"
    return payload


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["rpc"]:
        if len(args) < 3:
            return 2
        return _rpc_cli(args[1], args[2])

    rom = Path(os.environ.get("GB_INSTANCE_ROM", ""))
    if not rom.is_file():
        print(json.dumps({"error": "ROM path is missing"}), file=sys.stderr)
        return 1
    playability = inspect_rom_playable(rom)
    if not playability.get("playable"):
        print(json.dumps(_boot_failure_payload(rom)), file=sys.stderr)
        return 1
    subdirectory = os.environ.get("GB_INSTANCE_SUBDIRECTORY", "local")
    idle = float(
        os.environ.get("GB_PYBOY_IDLE_TIMEOUT_SECONDS", str(DEFAULT_IDLE_TIMEOUT_SECONDS))
    )
    speed_raw = os.environ.get("GB_PYBOY_EMULATION_SPEED", str(DEFAULT_EMULATION_SPEED)).strip()
    try:
        emulation_speed = int(speed_raw)
    except ValueError:
        emulation_speed = DEFAULT_EMULATION_SPEED
    session = EmulatorSession(
        email="instance",
        subdirectory=subdirectory,
        rom_path=rom,
        pyboy_factory=_default_pyboy_factory,
        idle_timeout_seconds=idle,
        emulation_speed=emulation_speed,
    )
    STATE.session = session
    session.start()
    try:
        session.wait_ready(timeout=30)
    except Exception as exc:
        print(json.dumps(_boot_failure_payload(rom, exc)), file=sys.stderr)
        return 1
    STATE.ready = True

    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    STATE.httpd = httpd
    thread = threading.Thread(target=httpd.serve_forever, name="instance-http", daemon=True)
    thread.start()

    # join() defaults to 15s (MCP host stop_pyboy). The instance process
    # must wait until idle/stop, otherwise the daemon PyBoy thread is killed.
    session.join(timeout=None)
    time.sleep(0.2)
    httpd.shutdown()
    return _exit_code(session.close_reason)


if __name__ == "__main__":
    raise SystemExit(main())
