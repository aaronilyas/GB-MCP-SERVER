"""Frozen play-loop limits and public field names.

Imported by the MCP host (no numpy / PyBoy) and by the play instance.
Do not put framebuffer math or emulator ticks in this module.
"""

from __future__ import annotations

# Native Game Boy LCD.
NATIVE_WIDTH = 160
NATIVE_HEIGHT = 144
NATIVE_SIZE = (NATIVE_WIDTH, NATIVE_HEIGHT)

# Valid joypad names (unchanged).
BUTTONS = frozenset({"a", "b", "start", "select", "up", "down", "left", "right"})

# Caps (raised for hour-scale sessions: 21600 frames ≈ 6 min at 1x).
MAX_INPUT_STEPS = 500
MAX_HOLD_FRAMES = 21600
MAX_FRAMES_PER_CALL = 21600
MAX_GAP_FRAMES = 180
MAX_UNTIL_EVAL_INTERVAL = 15
MIN_UNTIL_EVAL_INTERVAL = 1
MAX_SCREENSHOT_ALL = 30
SCREENSHOT_SCALES = frozenset({1, 2, 3, 4})
EMULATION_SPEEDS = frozenset({0, 1, 2, 4, 8})

# Screenshot / macro vocabularies.
SCREENSHOT_MODES = frozenset({"final", "all", "interrupt_and_final", "keyframes"})
MACROS = frozenset({"hold", "mash", "steps", "buttons"})
UNTIL_ONS = frozenset(
    {
        "pixel_delta_above",
        "pixel_delta_below",
        "stable",
        "region_hash_eq",
        "region_hash_neq",
        "classifier",
        "luma_jump",
        "blocked",
        "overworld",
        "none",
    }
)
CLASSIFIERS = frozenset({"textbox_likely", "battle_likely", "start_menu_likely"})
CLASSIFIER_POLARITIES = frozenset({"appears", "disappears"})
STOP_REASONS = frozenset(
    {
        "completed",
        "screen_change",
        "stable",
        "hash_match",
        "hash_mismatch",
        "classifier",
        "max_frames",
        "default_hold_abort",
        "call_timeout",
        "idle_timeout",
        "blocked",
        "fade",
        "overworld",
        "cap",
    }
)
PUBLIC_STOPPED_REASONS = frozenset(
    {
        "completed",
        "battle",
        "textbox",
        "menu",
        "fade",
        "blocked",
        "max_frames",
        "timeout",
        "overworld",
        "cap",
    }
)
PUBLIC_INTENTS = frozenset(
    {
        "advance_text",
        "run_away",
        "battle_turn",
        "battle_until_overworld",
        "skip_intro",
        "enter_door",
    }
)
PUBLIC_KEYFRAME_MIN_FRAMES = 24
# Mash / directional hold at or above this many planned frames may pack a GIF
# when the caller opts in with media=video. Not a default observation.
LONG_ACTION_FRAMES = 30
# Public single D-pad with frames omitted: walk hold (not a one-tile tap).
PUBLIC_DPAD_HOLD_FRAMES = 1800

# Defaults (long-run / cheap-media: uncapped, native PNG, 3-hour idle).
DEFAULT_EMULATION_SPEED = 0  # uncapped; pyboy.set_emulation_speed(0)
DEFAULT_SCREENSHOT_SCALE = 1  # native 160x144
DEFAULT_SCREENSHOT_MODE = "final"
DEFAULT_IDLE_TIMEOUT_SECONDS = 10800  # 3 hours
DEFAULT_UNTIL_EVAL_INTERVAL = 4
DEFAULT_UNTIL_THRESHOLD = 0.08
DEFAULT_STABLE_FRAMES = 12
DEFAULT_HOLD_ABORT_THRESHOLD = 0.12
# Mean-luminance jump (0–255) that counts as a fade for the default hold-abort second gate.
DEFAULT_HOLD_ABORT_LUMA_JUMP = 80.0
DEFAULT_MASH_BUTTON = "a"
DEFAULT_MASH_PRESS_FRAMES = 4
DEFAULT_MASH_RELEASE_FRAMES = 4
# Public PlayArgs mash only (dialogue typewriter skip). Engine PlayInput stays 4/4.
PUBLIC_MASH_PRESS_FRAMES = 12
PUBLIC_MASH_RELEASE_FRAMES = 8
DEFAULT_GAP_FRAMES = 0
DEFAULT_CALL_TIMEOUT_SECONDS = 90.0
MAX_CALL_TIMEOUT_SECONDS = 180.0
# off/image: one native 160x144 PNG. video: GIF packing allowed.
MEDIA_MODES = frozenset({"off", "image", "video"})
DEFAULT_MEDIA = "image"
DEFAULT_INPUT_LOG_NAME = "input_log.jsonl"

# Named hash boxes in native 160x144 space (inclusive origin, exclusive of x+w / y+h).
DEFAULT_REGION = (0, 0, NATIVE_WIDTH, NATIVE_HEIGHT)
BOTTOM_REGION = (0, 96, 160, 48)
CENTER_REGION = (40, 32, 80, 80)
# Larger center crop for wall-block detection (walk-cycle bob is averaged out).
PLAYER_BLOCKED_REGION = (48, 40, 64, 64)
DEFAULT_HASH_REGIONS: dict[str, tuple[int, int, int, int]] = {
    "full": DEFAULT_REGION,
    "bottom": BOTTOM_REGION,
    "center": CENTER_REGION,
}
# Blocked: coarse center-crop still vs previous eval. NPCs outside the crop are ignored.
BLOCKED_BLOCK_SIZE = 8
BLOCKED_CELL_TOLERANCE = 20
# Mean |RGB| of 8×8 cells. Walk-cycle bob is ~1.1; 1px camera scroll on textured
# overworld is ~3.3. Stuck if consecutive evals stay at or below this.
BLOCKED_COARSE_L1 = 2.5
# Facing turn is ~8–16 frames. With default until_eval_interval=4, four evals ≈ 16 frames.
BLOCKED_TURN_GRACE_EVALS = 4
# Released ticks after public mash aborts so the next A does not re-talk.
PUBLIC_MASH_ABORT_GAP_FRAMES = 12
# Luminance std below this ≈ solid fade / black / white (not a stable room).
UNIFORM_LUMA_STD_MAX = 6.0

# Command wait covers a default long hold; SessionManager still raises this
# to call_timeout_seconds + slack when the engine budget is larger.
INPUT_COMMAND_TIMEOUT_SECONDS = 120.0

# Keys that must never appear in tool JSON (case-insensitive substring check
# on the flattened key path). Framebuffer hashes / classifiers / PNGs are OK.
FORBIDDEN_RESPONSE_KEY_NEEDLES = (
    "wram",
    "hram",
    "memory",
    "mem_peek",
    "pyboy.memory",
    "party",
    "map_id",
    "mapid",
    "player_x",
    "player_y",
    "playerx",
    "playery",
    "symbols",
    "tilemap",
    "sprite_data",
    "battle_struct",
    "gamestate",
    "game_state",
    "ram_dump",
)

# Allowlisted JSON keys on a successful send_pyboy_input status dict.
# Scaled preview images travel beside the public play dict as MCP Image objects.
# Native 160x144 PNGs travel in-process as `pngs_native` (bytes) / Docker
# `pngs_native_b64`, and on the public play status as `screenshots[].png_base64`.
# `pngs` is the scaled preview list; the MCP host keeps the last frame as Image.
SEND_INPUT_RESPONSE_KEYS = frozenset(
    {
        "email",
        "subdirectory",
        "rom",
        "rom_path",
        "running",
        "saved",
        "close_reason",
        "restored_state",
        "restore_error",
        "idle_timeout_seconds",
        "seconds_until_idle_close",
        "seconds_since_last_input",
        "cartridge_title",
        "sent",
        "steps",
        "screenshot_mode",
        "screenshot_count",
        "screenshots",
        "screenshots_subsampled",
        "pngs",
        "pngs_native",
        "pngs_native_b64",
        "stop_reason",
        "frames_advanced",
        "emulation_speed",
        "until_fired",
        "region_hashes",
        "classifiers",
        "screenshot_scale",
        "native_size",
        "interrupt_frame_index",
        "default_hold_abort_applied",
        "macro",
        "gap_frames",
        "until_eval_interval",
        "ocr_text",
        "ocr_engine",
        "ocr_error",
        "error",
        "stop_detail",
        "player_moved",
        "textbox_complete",
    }
)
