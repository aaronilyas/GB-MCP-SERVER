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

# Caps (raised from 30 steps / 120 hold-frames).
MAX_INPUT_STEPS = 500
MAX_HOLD_FRAMES = 3600
MAX_FRAMES_PER_CALL = 3600
MAX_GAP_FRAMES = 60
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
    }
)

# Defaults (breaking vs the previous 1x / 5-minute / native-PNG path).
DEFAULT_EMULATION_SPEED = 0  # uncapped; pyboy.set_emulation_speed(0)
DEFAULT_SCREENSHOT_SCALE = 4  # nearest-neighbor integer upscale
DEFAULT_SCREENSHOT_MODE = "final"
DEFAULT_IDLE_TIMEOUT_SECONDS = 2700  # 45 minutes
DEFAULT_UNTIL_EVAL_INTERVAL = 4
DEFAULT_UNTIL_THRESHOLD = 0.08
DEFAULT_STABLE_FRAMES = 12
DEFAULT_HOLD_ABORT_THRESHOLD = 0.12
# Mean-luminance jump (0–255) that counts as a fade for the default hold-abort second gate.
DEFAULT_HOLD_ABORT_LUMA_JUMP = 80.0
DEFAULT_MASH_BUTTON = "a"
DEFAULT_MASH_PRESS_FRAMES = 4
DEFAULT_MASH_RELEASE_FRAMES = 4
DEFAULT_GAP_FRAMES = 0
DEFAULT_CALL_TIMEOUT_SECONDS = 20.0
MAX_CALL_TIMEOUT_SECONDS = 70.0

# Named hash boxes in native 160x144 space (inclusive origin, exclusive of x+w / y+h).
DEFAULT_REGION = (0, 0, NATIVE_WIDTH, NATIVE_HEIGHT)
BOTTOM_REGION = (0, 96, 160, 48)
CENTER_REGION = (40, 32, 80, 80)
DEFAULT_HASH_REGIONS: dict[str, tuple[int, int, int, int]] = {
    "full": DEFAULT_REGION,
    "bottom": BOTTOM_REGION,
    "center": CENTER_REGION,
}

# Command wait is slightly above the engine wall-clock so the engine can
# return stop_reason=call_timeout instead of raising TimeoutError.
INPUT_COMMAND_TIMEOUT_SECONDS = DEFAULT_CALL_TIMEOUT_SECONDS + 5.0

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
# Images travel beside this dict as MCP Image objects, not as these keys.
# `pngs` is an in-process list of PNG bytes popped before the MCP return.
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
    }
)
