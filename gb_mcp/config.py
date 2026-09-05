"""Process-wide paths and limits for the host MCP server."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from gb_mcp.gb.constants import MAX_ROM_BYTES

# Decoded bytes per append_gb_rom_upload chunk (~32 KiB base64 at 24 KiB).
DEFAULT_ROM_UPLOAD_CHUNK_BYTES = 24 * 1024
ROM_UPLOAD_TTL_SECONDS = 30 * 60

ROOT = Path(__file__).resolve().parent.parent
ROMS_DIR = ROOT / "roms"
DOCKER_IMAGE = os.environ.get("GB_ROM_VALIDATOR_IMAGE", "gb-rom-validator:latest")
INSTANCE_IMAGE = os.environ.get("GB_PYBOY_INSTANCE_IMAGE", "gb-pyboy-instance:latest")
# Base64 expands 3 bytes -> 4 chars; reject before decode to bound host memory.
MAX_ROM_B64_CHARS = (MAX_ROM_BYTES + 2) // 3 * 4
# Close a PyBoy session after this many seconds with no model button input.
# Default 45 minutes. Ping and input both reset the idle timer.
IDLE_TIMEOUT_SECONDS = int(os.environ.get("GB_PYBOY_IDLE_TIMEOUT_SECONDS", "2700"))
# Session-start emulation speed (0 = uncapped). Play instances also read this env.
EMULATION_SPEED = int(os.environ.get("GB_PYBOY_EMULATION_SPEED", "0"))
PYBOY_WINDOW = os.environ.get("GB_PYBOY_WINDOW", "null")


def rom_upload_chunk_bytes() -> int:
    """Decoded bytes per chunked-upload append. Env ``GB_ROM_UPLOAD_CHUNK_BYTES``."""
    raw = os.environ.get("GB_ROM_UPLOAD_CHUNK_BYTES", "").strip()
    if not raw:
        value = DEFAULT_ROM_UPLOAD_CHUNK_BYTES
    else:
        try:
            value = int(raw)
        except ValueError:
            value = DEFAULT_ROM_UPLOAD_CHUNK_BYTES
    return max(1024, min(value, MAX_ROM_BYTES))


def rom_uploads_dir() -> Path:
    """Staging directory for chunked ROM ingest (outside roms/<32-hex>/)."""
    return ROMS_DIR / ".uploads"


def roms_host_path() -> Path:
    """Directory the Docker daemon should bind for `roms/`.

    Inside `gb-mcp-server` this is the host path (`GB_ROMS_HOST_PATH`) so
    sibling play/validator containers mount the real directory, not `/app/roms`.
    On a host `python server.py` run it is `ROMS_DIR`.
    """
    raw = os.environ.get("GB_ROMS_HOST_PATH", "").strip()
    return Path(raw) if raw else ROMS_DIR

_HTTP_TRANSPORTS = frozenset({"http", "streamable-http", "streamable_http"})


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def http_host() -> str:
    """Bind address for Streamable HTTP. Read at call time, never at import."""
    return _env("GB_MCP_HOST", "0.0.0.0") or "0.0.0.0"


def http_port() -> int:
    """TCP port for Streamable HTTP. Read at call time, never at import."""
    raw = _env("GB_MCP_PORT", "8080") or "8080"
    return int(raw)


def http_path() -> str:
    """URL path for the MCP endpoint (default /mcp)."""
    path = _env("GB_MCP_PATH", "/mcp") or "/mcp"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/mcp"


def public_url() -> str | None:
    """Absolute public origin, if configured.

    Used only for absolute links and RFC 9728 protected-resource metadata.
    Unset is valid: callers derive the origin from the request Host header.
    """
    value = _env("GB_MCP_PUBLIC_URL").rstrip("/")
    return value or None


def bearer_token() -> str | None:
    """Shared secret accepted as ``Authorization: Bearer <token>``."""
    return _env("GB_MCP_BEARER_TOKEN") or None


def jwt_secret() -> str | None:
    """HS256 secret used to verify PyJWT-signed bearer tokens."""
    return _env("GB_MCP_JWT_SECRET") or None


def token_signing_secret() -> str | None:
    """HS256 key for OAuth access tokens (and operator JWTs when configured).

    Prefer ``GB_MCP_JWT_SECRET``. If only ``GB_MCP_BEARER_TOKEN`` is set, derive
    a stable key from it so hosted OAuth clients can still obtain JWTs.
    """
    secret = jwt_secret()
    if secret:
        return secret
    bearer = bearer_token()
    if not bearer:
        return None
    return hmac.new(b"gb-mcp-oauth-as", bearer.encode("utf-8"), hashlib.sha256).hexdigest()


def cors_origins() -> list[str]:
    """Browser Origins allowed to call MCP, well-known, and OAuth HTTP routes.

    Empty (default) is ``*`` so hosted LLM UIs (ChatGPT, Claude.ai) can
    preflight. ``none`` disables CORS. ``*`` allows any Origin without
    credentials. Comma-separated list otherwise.
    """
    raw = _env("GB_MCP_CORS_ORIGINS")
    if not raw:
        return ["*"]
    if raw.lower() == "none":
        return []
    if raw == "*":
        return ["*"]
    return [part.strip() for part in raw.split(",") if part.strip()]


def http_transport_requested() -> bool:
    """True when env asks for Streamable HTTP instead of stdio."""
    return _env("GB_MCP_TRANSPORT").lower() in _HTTP_TRANSPORTS
