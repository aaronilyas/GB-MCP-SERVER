"""Framebuffer interrupts and screenshot packaging. Implemented by sub-agent B.

The public contract is defined by input_schema and play_limits. Must not read
emulator memory.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from gb_mcp.emulator.play_limits import (
    BLOCKED_BLOCK_SIZE,
    BLOCKED_COARSE_L1,
    BLOCKED_TURN_GRACE_EVALS,
    DEFAULT_HASH_REGIONS,
    DEFAULT_HOLD_ABORT_LUMA_JUMP,
    DEFAULT_HOLD_ABORT_THRESHOLD,
    DEFAULT_REGION,
    DEFAULT_SCREENSHOT_SCALE,
    DEFAULT_STABLE_FRAMES,
    MAX_SCREENSHOT_ALL,
    NATIVE_HEIGHT,
    NATIVE_WIDTH,
    PLAYER_BLOCKED_REGION,
    SCREENSHOT_SCALES,
    UNIFORM_LUMA_STD_MAX,
)

# Channel delta ignored as encoder/LCD noise when comparing frames.
_PIXEL_DELTA_TOLERANCE = 8
_KEYFRAME_FRACS = (0.25, 0.50, 0.75, 1.0)
_PNG_FORMAT = "PNG"

# Classifier thresholds are coarse (synthetic 160x144 fixtures, not ROM dumps).
# Dialogue / prompt: framed light window (bottom third or lower-center overlay).
_TEXTBOX_BOTTOM_Y0 = 88
_TEXTBOX_BORDER_MAX = 90.0
_TEXTBOX_INNER_MIN = 130.0  # typewriter-partial lines still count
_TEXTBOX_CONTRAST_MIN = 50.0
_BATTLE_SPLIT_MIN = 25.0
_BAR_ROW_LUM_MIN = 190.0
# Contiguous status-bar run length in native pixels (many RPGs use ~40–100px tracks).
_BAR_MIN_RUN = 40
_BAR_MAX_RUN = 112
_BAR_THICKNESS = (2, 10)
_MENU_LIGHT_MIN = 200.0
# Tall near-white UI pane (pause / inventory), not a brick facade or house wall.
_MENU_LIGHT_FRAC = 0.70
_MENU_HEIGHT_FRAC = 0.75
_MENU_ROW_LIGHT_FRAC = 0.65
# Pane must clearly out-luma the rest (textured walls stay below this).
_MENU_PANE_DELTA = 24.0


# Stale GB window / warp slab: large near-black rectangle; rugs are dimmer brown.
_OCCLUDE_BLACK_LUM = 12.0
_OCCLUDE_FILL_FRAC = 0.90
_OCCLUDE_MIN_AREA_FRAC = 0.25
_OCCLUDE_FADE_FRAC = 0.88
_OCCLUDE_ROOM_STD_MIN = 6.0
_OCCLUDE_ROOM_BLACK_MAX = 0.40
# Gate expensive near-black rect search unless black grew or mean luma dropped.
_OCCLUDE_LUMA_DROP = 8.0
_OCCLUDE_BLACK_FRAC_RISE = 0.04
_TILE_BLOCK = 8
_TILE_NEIGHBOR_L1 = 18.0
_TILE_SAME_FRAC = 0.55
_DPAD_BUTTONS = frozenset({"up", "down", "left", "right"})
# Player sprite crop vs slightly larger background crop (facing-turn vs walk).
_PLAYER_SPRITE_REGION = (64, 56, 32, 32)
_PLAYER_BG_REGION = (40, 32, 80, 80)
_SPRITE_MOVE_L1 = 8.0
_BG_MOVE_L1 = BLOCKED_COARSE_L1


def _as_rgb(frame: Any) -> np.ndarray:
    """Return uint8 HxWx3 RGB, dropping an alpha channel when present."""
    if isinstance(frame, Image.Image):
        image = frame.convert("RGB")
        arr = np.asarray(image, dtype=np.uint8)
    else:
        arr = np.asarray(frame)
        if arr.ndim == 2:
            arr = np.stack((arr, arr, arr), axis=-1)
        if arr.ndim != 3 or arr.shape[-1] < 3:
            raise ValueError("frame must be RGB(A) with shape (H, W, 3+) or a PIL image")
        if arr.shape[-1] > 3:
            arr = arr[..., :3]
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr, dtype=np.uint8)


def _luminance(rgb: np.ndarray) -> np.ndarray:
    r = rgb[..., 0].astype(np.float32)
    g = rgb[..., 1].astype(np.float32)
    b = rgb[..., 2].astype(np.float32)
    return 0.299 * r + 0.587 * g + 0.114 * b


def _crop(rgb: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = (int(part) for part in box)
    height, width = rgb.shape[:2]
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(width, x + w)
    y1 = min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        return np.empty((0, 0, 3), dtype=np.uint8)
    return rgb[y0:y1, x0:x1]


def capture_native(pyboy: Any) -> np.ndarray:
    """Copy the composited LCD as contiguous (144, 160, 3) uint8 RGB.

    ``screen.ndarray`` is a view of PyBoy's RGBA ``_screenbuffer``, not a raw
    BG/window layer. ``screen.image`` can alias the same buffer.
    """
    screen = getattr(pyboy, "screen", None)
    if screen is None:
        raise RuntimeError("PyBoy screen is unavailable")
    frame: Any = None
    raw = getattr(screen, "ndarray", None)
    if raw is not None:
        try:
            candidate = np.array(np.asarray(raw), copy=True, order="C")
            if getattr(candidate, "ndim", 0) >= 2:
                frame = candidate
        except Exception:
            frame = None
    if frame is None:
        image = getattr(screen, "image", None)
        if image is None:
            raise RuntimeError("PyBoy screen image is unavailable")
        if isinstance(image, Image.Image):
            frame = image.copy()
        else:
            frame = np.array(np.asarray(image), copy=True, order="C")
    return np.array(_as_rgb(frame), dtype=np.uint8, copy=True, order="C")


def scale_nearest(frame: Any, scale: int) -> Image.Image:
    """Integer nearest-neighbor upscale. `scale` must be 1, 2, 3, or 4."""
    if scale not in SCREENSHOT_SCALES:
        raise ValueError("screenshot_scale must be 1, 2, 3, or 4")
    rgb = _as_rgb(frame)
    if scale > 1:
        rgb = np.repeat(np.repeat(rgb, scale, axis=0), scale, axis=1)
    return Image.fromarray(rgb)


def encode_png(image: Image.Image) -> bytes:
    if image.mode != "RGB":
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format=_PNG_FORMAT)
    data = buf.getvalue()
    if not data:
        raise RuntimeError("failed to encode PNG")
    return data


def region_hash(frame: Any, box: tuple[int, int, int, int]) -> str:
    """blake2s hex digest (digest_size=8) of contiguous native RGB crop bytes."""
    crop = np.ascontiguousarray(_crop(_as_rgb(frame), box), dtype=np.uint8)
    return hashlib.blake2s(crop.tobytes(), digest_size=8).hexdigest()


def pixel_delta_fraction(
    baseline: Any, current: Any, region: tuple[int, int, int, int]
) -> float:
    """Fraction of region pixels with any RGB channel delta > 8 vs baseline."""
    a = _crop(_as_rgb(baseline), region)
    b = _crop(_as_rgb(current), region)
    count = int(a.shape[0] * a.shape[1])
    if count == 0 or b.shape[:2] != a.shape[:2]:
        return 0.0
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    changed = np.any(delta > _PIXEL_DELTA_TOLERANCE, axis=-1)
    return float(np.count_nonzero(changed) / count)


def hash_named_regions(
    frame: Any, regions: dict[str, tuple[int, int, int, int]]
) -> dict[str, str]:
    rgb = _as_rgb(frame)
    return {name: region_hash(rgb, box) for name, box in regions.items()}


def _near_uniform(rgb: np.ndarray, *, std_max: float = UNIFORM_LUMA_STD_MAX) -> bool:
    """Near-solid black/white/fade: luminance variance too low for a playfield."""
    if rgb.size == 0:
        return True
    return float(_luminance(rgb).std()) < std_max


def _framed_light_window(
    rgb: np.ndarray, *, y0: int, y1: int | None = None, x0: int = 0, x1: int | None = None
) -> tuple[bool, tuple[int, int, int, int] | None]:
    """Dark/high-contrast rectangular frame with a much lighter inner fill."""
    height, width = rgb.shape[:2]
    y1 = height if y1 is None else y1
    x1 = width if x1 is None else x1
    if y1 - y0 < 24 or x1 - x0 < 40:
        return False, None
    region = rgb[y0:y1, x0:x1]
    lum = _luminance(region)
    rh, rw = lum.shape
    if rh < 16 or rw < 24:
        return False, None
    border = np.concatenate(
        (lum[:3, :].ravel(), lum[-3:, :].ravel(), lum[:, :3].ravel(), lum[:, -3:].ravel())
    )
    inner = lum[4 : rh - 4, 6 : rw - 6]
    if inner.size == 0:
        return False, None
    border_mean = float(border.mean())
    inner_mean = float(inner.mean())
    if not (
        border_mean < _TEXTBOX_BORDER_MAX
        and inner_mean > _TEXTBOX_INNER_MIN
        and (inner_mean - border_mean) > _TEXTBOX_CONTRAST_MIN
    ):
        return False, None
    # Full-screen white flash is not a textbox.
    if y0 <= 4 and y1 >= height - 4 and x0 <= 4 and x1 >= width - 4:
        if float(lum.mean()) > 220.0 and float(lum.std()) < 25.0:
            return False, None
    # Thin HP bar tracks are not dialogue windows.
    if rh <= 14:
        return False, None
    inner_box = (x0 + 6, y0 + 4, max(1, rw - 12), max(1, rh - 8))
    return True, inner_box


def textbox_inner_rect(frame: Any) -> tuple[int, int, int, int] | None:
    """Native (x, y, w, h) of the textbox inner fill, or None."""
    rgb = _as_rgb(frame)
    ok, box = _textbox_rect(rgb)
    return box if ok else None


def _textbox_rect(rgb: np.ndarray) -> tuple[bool, tuple[int, int, int, int] | None]:
    if rgb.shape[0] < NATIVE_HEIGHT or rgb.shape[1] < NATIVE_WIDTH:
        return False, None
    # Prefer the classic bottom dialogue strip (y≈96), then a slightly taller bottom third.
    for y0 in (96, _TEXTBOX_BOTTOM_Y0):
        ok, box = _framed_light_window(rgb, y0=y0)
        if ok:
            return True, box
    # Centered / lower-center overlay (many adventure-game prompts).
    ok, box = _framed_light_window(rgb, y0=48, y1=120, x0=16, x1=144)
    if ok:
        return True, box
    ok, box = _framed_light_window(rgb, y0=56, y1=128, x0=8, x1=152)
    if ok:
        return True, box
    return False, None


def _textbox_likely(rgb: np.ndarray) -> bool:
    """Framed light dialogue/prompt window — not a full-screen flash or HP bar."""
    ok, _box = _textbox_rect(rgb)
    return ok


def _longest_bright_run(row: np.ndarray) -> int:
    """Longest consecutive run of True in a 1-d mask."""
    best = 0
    current = 0
    for flag in row.tolist():
        if flag:
            current += 1
            if current > best:
                best = current
        else:
            current = 0
    return best


def _bar_like_run(row: np.ndarray, min_run: int, max_run: int) -> bool:
    """Compact bright track on an otherwise darker row — not a light wall band."""
    flags = row.tolist()
    best = 0
    best_start = 0
    current = 0
    start = 0
    for index, flag in enumerate(flags):
        if flag:
            if current == 0:
                start = index
            current += 1
            if current > best:
                best = current
                best_start = start
        else:
            current = 0
    if not (min_run <= best <= max_run):
        return False
    width = len(flags)
    best_end = best_start + best
    # Light facades light a half-screen band flush to an edge; status bars are inset tracks.
    edge_flush = best_start == 0 or best_end == width
    if edge_flush and best >= int(width * 0.45):
        return False
    if float(row.mean()) > 0.80:
        return False
    return True


def _light_horizontal_strips(lum: np.ndarray) -> list[tuple[int, int]]:
    """Thin compact status-bar runs (height 2–10), not pavement or full-width ledges."""
    if lum.size == 0:
        return []
    width = int(lum.shape[1])
    scale = width / float(NATIVE_WIDTH) if width else 1.0
    min_run = max(1, int(round(_BAR_MIN_RUN * scale)))
    max_run = max(min_run, int(round(_BAR_MAX_RUN * scale)))
    bright = lum >= _BAR_ROW_LUM_MIN
    is_bar = np.array(
        [_bar_like_run(row, min_run, max_run) for row in bright],
        dtype=bool,
    )
    strips: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(is_bar.tolist()):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            strips.append((start, index - start))
            start = None
    if start is not None:
        strips.append((start, int(is_bar.shape[0]) - start))
    lo, hi = _BAR_THICKNESS
    return [item for item in strips if lo <= item[1] <= hi]


def _strip_in_band(strips: list[tuple[int, int]], y0: int, y1: int) -> bool:
    """True if a light strip is centered in [y0, y1)."""
    return any(y0 <= start + thickness // 2 < y1 for start, thickness in strips)


def _command_pane_rect(rgb: np.ndarray) -> tuple[int, int, int, int] | None:
    """Light command rectangle (often bottom-right or bottom), or None."""
    if rgb.shape[0] < 96 or rgb.shape[1] < 96:
        return None
    candidates: list[tuple[int, int, int, int]] = [
        (80, 80, 80, 64),  # bottom-right
        (0, 96, 160, 48),  # bottom strip
        (80, 96, 80, 48),  # bottom-right lower
        (0, 80, 80, 64),  # bottom-left
    ]
    best: tuple[int, int, int, int] | None = None
    best_delta = 0.0
    for x, y, w, h in candidates:
        pane = rgb[y : y + h, x : x + w]
        if pane.size == 0:
            continue
        pane_lum = _luminance(pane)
        if float((pane_lum >= _MENU_LIGHT_MIN).mean()) < 0.40:
            continue
        row_slice = rgb[y : y + h]
        if x >= 40:
            rest = row_slice[:, : max(1, x)]
        else:
            rest = row_slice[:, min(rgb.shape[1], x + w) :]
        if rest.size == 0:
            continue
        rest_lum = _luminance(rest)
        delta = float(pane_lum.mean()) - float(rest_lum.mean())
        if delta >= 20.0 and delta > best_delta:
            best_delta = delta
            best = (x, y, w, h)
    return best


def _command_pane_likely(rgb: np.ndarray) -> bool:
    return _command_pane_rect(rgb) is not None


def _tilemap_field_likely(rgb: np.ndarray) -> bool:
    """Repeating 8×8-ish playfield with no command pane and no dual status bars."""
    if _command_pane_likely(rgb):
        return False
    lum = _luminance(rgb)
    strips = _light_horizontal_strips(lum)
    height = lum.shape[0]
    third = max(1, height // 3)
    dual = _strip_in_band(strips, 0, third) and _strip_in_band(
        strips, height // 2, height * 5 // 6
    )
    if dual:
        return False
    if _textbox_likely(rgb):
        return False
    # Avoid recursion into start_menu via classify; check pane geometry only.
    mid = max(1, rgb.shape[1] // 2)
    if _menu_pane_likely(rgb[:, :mid], rgb[:, mid:]) or _menu_pane_likely(
        rgb[:, mid:], rgb[:, :mid]
    ):
        return False
    block = _TILE_BLOCK
    rows = height // block
    cols = lum.shape[1] // block
    if rows < 4 or cols < 4:
        return False
    means = (
        rgb[: rows * block, : cols * block]
        .reshape(rows, block, cols, block, 3)
        .mean(axis=(1, 3))
        .astype(np.float32)
    )
    same = 0
    total = 0
    for y in range(rows):
        for x in range(cols - 1):
            total += 1
            if float(np.mean(np.abs(means[y, x] - means[y, x + 1]))) < _TILE_NEIGHBOR_L1:
                same += 1
        if y < rows - 1:
            for x in range(cols):
                total += 1
                if float(np.mean(np.abs(means[y, x] - means[y + 1, x]))) < _TILE_NEIGHBOR_L1:
                    same += 1
    if total <= 0:
        return False
    return (same / float(total)) >= _TILE_SAME_FRAC


def _dual_status_bars(rgb: np.ndarray) -> bool:
    """Two separated thin bright bar-like runs (upper vs lower status clusters)."""
    lum = _luminance(rgb)
    height = lum.shape[0]
    strips = _light_horizontal_strips(lum)
    third = max(1, height // 3)
    return _strip_in_band(strips, 0, third) and _strip_in_band(
        strips, height // 2, height * 5 // 6
    )


def _palette_split(rgb: np.ndarray) -> float:
    height = rgb.shape[0]
    third = max(1, height // 3)
    top_mean = rgb[:third].reshape(-1, 3).mean(axis=0)
    bot_mean = rgb[-third:].reshape(-1, 3).mean(axis=0)
    vertical = float(np.abs(top_mean - bot_mean).mean())
    mid = max(1, rgb.shape[1] // 2)
    left_mean = rgb[:, :mid].reshape(-1, 3).mean(axis=0)
    right_mean = rgb[:, mid:].reshape(-1, 3).mean(axis=0)
    horizontal = float(np.abs(left_mean - right_mean).mean())
    return max(vertical, horizontal)


def _battle_likely(rgb: np.ndarray) -> bool:
    """Combat HUD: dual status clusters and/or a command pane — not a scrolling tilemap."""
    # textbox/start-menu helpers must not call back into _battle_likely.
    if _textbox_likely(rgb) or _start_menu_likely(rgb):
        return False
    if _tilemap_field_likely(rgb):
        return False
    split = _palette_split(rgb)
    pane = _command_pane_likely(rgb)
    dual = _dual_status_bars(rgb)
    # Sufficient pattern: dual slotted bars + (command pane or strong split).
    if dual and (pane or split > _BATTLE_SPLIT_MIN):
        return True
    # Framed command pane on a distinct HUD (split playfield, not overworld).
    if pane and split > _BATTLE_SPLIT_MIN:
        return True
    return False


def _prompt_triangle(patch: np.ndarray) -> bool:
    if patch.size == 0:
        return False
    dark = patch < 90.0
    frac = float(dark.mean())
    if frac < 0.08 or frac > 0.50:
        return False
    hh = int(dark.shape[0])
    upper = dark[: max(1, hh // 2)]
    lower = dark[max(0, hh - 2) :]
    if int(upper.sum()) < 6 or int(upper.sum()) <= int(lower.sum()):
        return False
    spans: list[int] = []
    for row in dark:
        idx = np.flatnonzero(row)
        spans.append(int(idx[-1] - idx[0] + 1) if idx.size else 0)
    nonzero = [span for span in spans if span > 0]
    if len(nonzero) < 3:
        return False
    return nonzero[0] >= 3 and nonzero[-1] < nonzero[0]


def _textbox_complete(rgb: np.ndarray) -> bool:
    """Dark ▼ / triangle in the inner textbox, after a finished line."""
    ok, inner = _textbox_rect(rgb)
    if not ok or inner is None:
        return False
    lum = _luminance(rgb)
    x, y, w, h = inner
    # Prompt glyph sits in the lower-right of the inner fill.
    patches = [
        lum[y + max(0, h - 14) : y + h, x + max(0, w - 24) : x + w],
        lum[128:138, 128:148],  # common bottom-box prompt band
    ]
    return any(_prompt_triangle(patch) for patch in patches)


def textbox_complete(frame: Any) -> bool:
    return _textbox_complete(_as_rgb(frame))


def _coarse_grid(
    rgb: np.ndarray,
    box: tuple[int, int, int, int] = PLAYER_BLOCKED_REGION,
    block: int = BLOCKED_BLOCK_SIZE,
) -> np.ndarray | None:
    """Mean RGB of each block×block cell in the center crop."""
    crop = _crop(rgb, box)
    height, width = crop.shape[:2]
    rows = height // block
    cols = width // block
    if rows <= 0 or cols <= 0:
        return None
    trimmed = crop[: rows * block, : cols * block]
    cells = trimmed.reshape(rows, block, cols, block, 3).mean(axis=(1, 3))
    return cells.astype(np.float32)


def coarse_mean_abs(
    baseline: Any,
    current: Any,
    region: tuple[int, int, int, int] = PLAYER_BLOCKED_REGION,
) -> float:
    """Mean absolute RGB of coarse center-crop cells (0–255)."""
    ga = _coarse_grid(_as_rgb(baseline), region)
    gb = _coarse_grid(_as_rgb(current), region)
    if ga is None or gb is None or ga.shape != gb.shape:
        return 0.0
    return float(np.mean(np.abs(ga - gb)))


def player_moved_from_frames(baseline: Any, current: Any) -> bool:
    """True when the camera/background moved. Facing-only sprite turns stay false."""
    base = _as_rgb(baseline)
    cur = _as_rgb(current)
    bg = coarse_mean_abs(base, cur, _PLAYER_BG_REGION)
    if bg > _BG_MOVE_L1:
        return True
    # Coarse player crop (legacy): full-center motion still counts as moved.
    return coarse_mean_abs(base, cur, PLAYER_BLOCKED_REGION) > BLOCKED_COARSE_L1


def player_facing_turned(baseline: Any, current: Any) -> bool:
    """Sprite pixels changed in the center but the background grid did not."""
    base = _as_rgb(baseline)
    cur = _as_rgb(current)
    sprite = coarse_mean_abs(base, cur, _PLAYER_SPRITE_REGION)
    bg = coarse_mean_abs(base, cur, _PLAYER_BG_REGION)
    return sprite > _SPRITE_MOVE_L1 and bg <= _BG_MOVE_L1


def command_pane_visible(frame: Any) -> bool:
    return _command_pane_likely(_as_rgb(frame))


def command_pane_bottom_right_path(frame: Any) -> list[str]:
    """D-pad + A toward the bottom-right cell of a detectable 2×2 command pane.

    Empty when no pane is visible (do not invent a game-specific menu graph).
    """
    rgb = _as_rgb(frame)
    rect = _command_pane_rect(rgb)
    if rect is None:
        return []
    cell = command_pane_cursor_cell(rgb)
    if cell == "br":
        return ["a"]
    if cell == "bl":
        return ["right", "a"]
    if cell == "tr":
        return ["down", "a"]
    if cell == "tl":
        return ["down", "right", "a"]
    # Unknown cursor: nudge toward bottom-right then confirm.
    return ["down", "right", "a"]


def command_pane_cursor_cell(frame: Any) -> str | None:
    """Which 2×2 command-pane cell holds a dark cursor, or None."""
    rgb = _as_rgb(frame)
    rect = _command_pane_rect(rgb)
    if rect is None:
        return None
    x, y, w, h = rect
    lum = _luminance(rgb)
    cells = {
        "tl": (x, y, w // 2, h // 2),
        "tr": (x + w // 2, y, w - w // 2, h // 2),
        "bl": (x, y + h // 2, w // 2, h - h // 2),
        "br": (x + w // 2, y + h // 2, w - w // 2, h - h // 2),
    }
    best_name: str | None = None
    best_score = 0.0
    for name, (cx, cy, cw, ch) in cells.items():
        cell = lum[cy : cy + ch, cx : cx + min(12, max(1, cw))]
        if cell.size == 0:
            continue
        dark = cell < 70.0
        score = float(dark.mean())
        if 0.08 <= score <= 0.70 and score > best_score:
            best_score = score
            best_name = name
    if best_score < 0.12:
        return None
    return best_name


def _menu_pane_likely(pane: np.ndarray, rest: np.ndarray) -> bool:
    """Light vertical UI slab covering most of the height, brighter than the rest."""
    if pane.size == 0 or rest.size == 0:
        return False
    pane_lum = _luminance(pane)
    rest_lum = _luminance(rest)
    light = pane_lum >= _MENU_LIGHT_MIN
    if float(light.mean()) < _MENU_LIGHT_FRAC:
        return False
    row_frac = light.mean(axis=1)
    if float((row_frac >= _MENU_ROW_LIGHT_FRAC).mean()) < _MENU_HEIGHT_FRAC:
        return False
    pane_mean = float(pane_lum.mean())
    rest_mean = float(rest_lum.mean())
    if pane_mean - rest_mean < _MENU_PANE_DELTA:
        return False
    return True


def _full_width_menu_likely(rgb: np.ndarray) -> bool:
    """Large high-contrast overlay covering most of the LCD (title / pause list)."""
    if rgb.ndim != 3:
        return False
    lum = _luminance(rgb)
    if _near_uniform(rgb) and float(lum.mean()) > 220.0:
        return False
    light = lum >= _MENU_LIGHT_MIN
    if float(light.mean()) < 0.55:
        return False
    # Stacked list: many horizontal light rows with darker gaps.
    row_frac = light.mean(axis=1)
    light_rows = row_frac >= 0.70
    if float(light_rows.mean()) < 0.45:
        return False
    transitions = int(np.count_nonzero(light_rows[1:] != light_rows[:-1]))
    return transitions >= 4


def _start_menu_likely(rgb: np.ndarray) -> bool:
    """Tall/large high-contrast overlay pane (pause / inventory / title menu)."""
    if rgb.ndim != 3 or rgb.shape[1] < 2:
        return False
    if _textbox_likely(rgb):
        return False
    if _near_uniform(rgb) and float(_luminance(rgb).mean()) > 220.0:
        return False
    mid = max(1, rgb.shape[1] // 2)
    left = rgb[:, :mid]
    right = rgb[:, mid:]
    if _menu_pane_likely(left, right) or _menu_pane_likely(right, left):
        return True
    return _full_width_menu_likely(rgb)


def _largest_near_black_rect(
    black: np.ndarray, fill: float
) -> tuple[int, int, int, int] | None:
    """Largest axis-aligned near-black rect flush with an edge or the bottom-right."""
    height, width = black.shape
    integ = np.zeros((height + 1, width + 1), dtype=np.float64)
    integ[1:, 1:] = np.cumsum(np.cumsum(black.astype(np.float64), axis=0), axis=1)

    def rect_fill(y0: int, y1: int, x0: int, x1: int) -> float:
        area = (y1 - y0) * (x1 - x0)
        if area <= 0:
            return 0.0
        total = integ[y1, x1] - integ[y0, x1] - integ[y1, x0] + integ[y0, x0]
        return float(total / area)

    best_box: tuple[int, int, int, int] | None = None
    best_area = 0

    def take(y0: int, y1: int, x0: int, x1: int) -> None:
        nonlocal best_box, best_area
        area = (y1 - y0) * (x1 - x0)
        if area <= best_area:
            return
        if rect_fill(y0, y1, x0, x1) < fill:
            return
        best_area = area
        best_box = (y0, y1, x0, x1)

    for y1 in range(1, height + 1):
        take(0, y1, 0, width)
    for y0 in range(height):
        take(y0, height, 0, width)
    for x1 in range(1, width + 1):
        take(0, height, 0, x1)
    for x0 in range(width):
        take(0, height, x0, width)
    # GB window is always the bottom-right from (WX-7, WY).
    for y0 in range(height):
        remain_h = height - y0
        if remain_h * width <= best_area:
            break
        for x0 in range(width):
            if remain_h * (width - x0) <= best_area:
                break
            take(y0, height, x0, width)
    return best_box


def _black_frac(rgb: np.ndarray) -> float:
    return float((_luminance(rgb) <= _OCCLUDE_BLACK_LUM).mean())


def _window_occluded_likely(
    rgb: np.ndarray,
    *,
    baseline: np.ndarray | None = None,
    force_rect: bool = False,
) -> bool:
    """Large near-black takeover slab; thin static letterbox edges are scenery."""
    if rgb.ndim != 3 or rgb.shape[0] < 1 or rgb.shape[1] < 1:
        return False
    lum = _luminance(rgb)
    black = lum <= _OCCLUDE_BLACK_LUM
    black_frac = float(black.mean())
    if black_frac >= _OCCLUDE_FADE_FRAC:
        return False
    if black_frac < _OCCLUDE_MIN_AREA_FRAC * _OCCLUDE_FILL_FRAC:
        return False
    # Gate the expensive rect search unless black grew / luma dropped vs baseline.
    if baseline is not None and not force_rect:
        base_lum = float(_luminance(baseline).mean())
        base_black = _black_frac(baseline)
        cur_lum = float(lum.mean())
        if not (
            (base_lum - cur_lum) >= _OCCLUDE_LUMA_DROP
            or (black_frac - base_black) >= _OCCLUDE_BLACK_FRAC_RISE
        ):
            return False
    elif not force_rect and baseline is None:
        # Standalone classify(): still run, but reject thin edge letterbox
        # (camera off-map bars) that leave most of the field textured.
        pass
    box = _largest_near_black_rect(black, _OCCLUDE_FILL_FRAC)
    if box is None:
        return False
    y0, y1, x0, x1 = box
    height, width = lum.shape
    area = (y1 - y0) * (x1 - x0)
    area_frac = area / float(height * width)
    if area_frac < _OCCLUDE_MIN_AREA_FRAC:
        return False
    # Thin letterbox strips on one edge are scenery, not a warp slab.
    strip_h = y1 - y0
    strip_w = x1 - x0
    edge_flush = y0 == 0 or y1 == height or x0 == 0 or x1 == width
    if edge_flush and area_frac < 0.35 and (strip_h <= 16 or strip_w <= 24):
        return False
    rest_mask = np.ones((height, width), dtype=bool)
    rest_mask[y0:y1, x0:x1] = False
    rest = lum[rest_mask]
    if rest.size == 0:
        return False
    if float((rest <= _OCCLUDE_BLACK_LUM).mean()) > _OCCLUDE_ROOM_BLACK_MAX:
        return False
    if float(rest.std()) < _OCCLUDE_ROOM_STD_MIN:
        return False
    return True


def classify(frame: Any, *, baseline: Any | None = None) -> dict[str, bool]:
    rgb = _as_rgb(frame)
    base = None if baseline is None else _as_rgb(baseline)
    # With a call baseline, gate the expensive near-black rect search.
    force_rect = baseline is None
    return {
        "textbox_likely": _textbox_likely(rgb),
        "battle_likely": _battle_likely(rgb),
        "start_menu_likely": _start_menu_likely(rgb),
        "window_occluded_likely": _window_occluded_likely(
            rgb, baseline=base, force_rect=force_rect
        ),
    }


_CLASSIFIER_PUBLIC = {
    "battle_likely": "battle",
    "textbox_likely": "textbox",
    "start_menu_likely": "menu",
}


class StopDecision:
    def __init__(
        self,
        reason: str,
        until_fired: bool = True,
        *,
        detail: str | None = None,
    ) -> None:
        self.reason = reason
        self.until_fired = until_fired
        self.detail = detail


class UntilMonitor:
    def __init__(self, play: Any, baseline_frame: Any) -> None:
        self.play = play
        self.baseline_frame = np.array(_as_rgb(baseline_frame), copy=True, order="C")
        self._prev_eval_frame: np.ndarray | None = None
        self._stable_streak = 0
        self._blocked_streak = 0
        self._classifier_seen_true = False
        self._disappear_met_at_baseline = False
        self._occluded_seen = False
        self._baseline_classifiers = classify(self.baseline_frame)
        self._baseline_luma = float(_luminance(self.baseline_frame).mean())
        self._baseline_black_frac = _black_frac(self.baseline_frame)
        # Static letterbox already on the baseline is scenery, not a warp-in-progress.
        self._baseline_occluded = bool(
            self._baseline_classifiers.get("window_occluded_likely")
        )
        self._occluded_seen = False
        until = getattr(play, "until", None)
        classifier = getattr(until, "classifier", None) if until is not None else None
        if until is not None and until.on == "classifier" and classifier:
            present = bool(self._baseline_classifiers.get(classifier))
            self._classifier_seen_true = present
            if until.classifier_polarity == "disappears" and not present:
                self._disappear_met_at_baseline = True

    def _classify(self, frame: np.ndarray) -> dict[str, bool]:
        return classify(frame, baseline=self.baseline_frame)

    def evaluate(self, frame: Any, eval_index: int) -> StopDecision | None:
        rgb = np.array(_as_rgb(frame), copy=True, order="C")
        caller = self._eval_caller_until(rgb)
        abort = self._eval_default_hold_abort(rgb, eval_index=eval_index)
        self._prev_eval_frame = rgb
        if caller is not None:
            return caller
        return abort

    def _directional_hold(self) -> bool:
        if getattr(self.play, "macro", None) != "hold":
            return False
        buttons = set(getattr(self.play, "buttons", ()) or ())
        return bool(buttons & _DPAD_BUTTONS)

    def _playable_luma(self, frame: np.ndarray) -> float:
        """Mean luma of non-near-black pixels (ignores static letterbox bars)."""
        lum = _luminance(frame)
        mask = lum > _OCCLUDE_BLACK_LUM
        if not np.any(mask):
            return float(lum.mean())
        return float(lum[mask].mean())

    def _fade_abort(self, frame: np.ndarray, *, delta: float, threshold: float) -> bool:
        if delta <= threshold:
            return False
        luma_limit = float(
            getattr(self.play, "default_hold_abort_luma_jump", DEFAULT_HOLD_ABORT_LUMA_JUMP)
        )
        # Full-screen uniform black/white.
        if _near_uniform(frame):
            return True
        # Luma jump on the playable (non-letterbox) region.
        playable_jump = abs(self._playable_luma(frame) - self._playable_luma(self.baseline_frame))
        if playable_jump > luma_limit:
            return True
        # Occlusion that appeared this call (not the baseline letterbox).
        occluded = bool(self._classify(frame).get("window_occluded_likely"))
        if occluded and not self._baseline_occluded:
            return True
        black_rise = _black_frac(frame) - self._baseline_black_frac
        if black_rise >= _OCCLUDE_BLACK_FRAC_RISE and playable_jump > luma_limit * 0.5:
            return True
        return False

    def _eval_default_hold_abort(
        self, frame: np.ndarray, *, eval_index: int = 0
    ) -> StopDecision | None:
        if not getattr(self.play, "apply_default_hold_abort", False):
            return None
        # Text / combat / menu / fade outrank blocked on the same eval frame.
        current = self._classify(frame)
        battle_became = (not self._baseline_classifiers.get("battle_likely")) and bool(
            current.get("battle_likely")
        )
        textbox_became = (not self._baseline_classifiers.get("textbox_likely")) and bool(
            current.get("textbox_likely")
        )
        menu_became = (not self._baseline_classifiers.get("start_menu_likely")) and bool(
            current.get("start_menu_likely")
        )
        threshold = float(
            getattr(self.play, "default_hold_abort_threshold", DEFAULT_HOLD_ABORT_THRESHOLD)
        )
        delta = pixel_delta_fraction(self.baseline_frame, frame, DEFAULT_REGION)
        # Combat abort requires a real HUD, not a tilemap field false positive.
        if battle_became and delta > threshold and not _tilemap_field_likely(frame):
            return StopDecision("default_hold_abort", True, detail="battle")
        if textbox_became and delta > threshold:
            return StopDecision("default_hold_abort", True, detail="textbox")
        if menu_became and delta > threshold:
            return StopDecision("default_hold_abort", True, detail="menu")
        if self._fade_abort(frame, delta=delta, threshold=threshold):
            return StopDecision("default_hold_abort", True, detail="fade")
        if self._directional_hold():
            blocked = self._eval_blocked(frame, as_until=False, eval_index=eval_index)
            if blocked is not None:
                return StopDecision("default_hold_abort", True, detail="blocked")
        return None

    def _blocked_needed(self, *, as_until: bool) -> int:
        until = getattr(self.play, "until", None)
        if as_until and until is not None and getattr(until, "stable_frames", None):
            return int(until.stable_frames)
        return int(DEFAULT_STABLE_FRAMES)

    def _is_blocked(self, frame: np.ndarray) -> bool:
        """Stuck walker: coarse player crop stable vs previous eval. Ignore the rest of the LCD."""
        if self._prev_eval_frame is None:
            return False
        return coarse_mean_abs(self._prev_eval_frame, frame) <= BLOCKED_COARSE_L1

    def _eval_blocked(
        self, frame: np.ndarray, *, as_until: bool, eval_index: int = 0
    ) -> StopDecision | None:
        if self._prev_eval_frame is None:
            self._blocked_streak = 0
            return None
        if not as_until and eval_index < BLOCKED_TURN_GRACE_EVALS:
            self._blocked_streak = 0
            return None
        if self._is_blocked(frame):
            self._blocked_streak += 1
        else:
            self._blocked_streak = 0
        if self._blocked_streak >= self._blocked_needed(as_until=as_until):
            return StopDecision("blocked", True, detail="blocked")
        return None

    def _eval_luma_jump(self, frame: np.ndarray) -> StopDecision | None:
        luma_limit = float(
            getattr(self.play, "default_hold_abort_luma_jump", DEFAULT_HOLD_ABORT_LUMA_JUMP)
        )
        playable_jump = abs(self._playable_luma(frame) - self._playable_luma(self.baseline_frame))
        occluded = bool(self._classify(frame).get("window_occluded_likely"))
        if occluded and not self._baseline_occluded:
            self._occluded_seen = True
        if _near_uniform(frame) or playable_jump > luma_limit:
            return StopDecision("fade", True, detail="fade")
        if self._occluded_seen and not occluded:
            return StopDecision("fade", True, detail="fade")
        return None

    def _eval_overworld(self, frame: np.ndarray, until: Any) -> StopDecision | None:
        flags = self._classify(frame)
        if bool(flags.get("battle_likely")) or bool(flags.get("textbox_likely")):
            self._stable_streak = 0
            return None
        # HUD gone but still on a solid fade/black — wait for texture.
        if _near_uniform(frame):
            self._stable_streak = 0
            return None
        if self._prev_eval_frame is None:
            return None
        region = until.region or DEFAULT_REGION
        delta = pixel_delta_fraction(self._prev_eval_frame, frame, region)
        if delta < until.threshold:
            self._stable_streak += 1
        else:
            self._stable_streak = 0
        if self._stable_streak >= until.stable_frames:
            return StopDecision("stable", True, detail="completed")
        return None

    def _eval_caller_until(self, frame: np.ndarray) -> StopDecision | None:
        until = getattr(self.play, "until", None)
        if until is None or until.on in {"none", None}:
            return None
        region = until.region or DEFAULT_REGION
        on = until.on
        if on == "pixel_delta_above":
            if pixel_delta_fraction(self.baseline_frame, frame, region) > until.threshold:
                return StopDecision("screen_change", detail="fade")
            return None
        if on == "pixel_delta_below":
            if pixel_delta_fraction(self.baseline_frame, frame, region) < until.threshold:
                return StopDecision("screen_change")
            return None
        if on == "stable":
            if self._prev_eval_frame is None:
                return None
            # Do not treat solid fade/black/white as a settled room.
            if _near_uniform(frame):
                self._stable_streak = 0
                return None
            delta = pixel_delta_fraction(self._prev_eval_frame, frame, region)
            if delta < until.threshold:
                self._stable_streak += 1
            else:
                self._stable_streak = 0
            if self._stable_streak >= until.stable_frames:
                return StopDecision("stable")
            return None
        if on == "region_hash_eq":
            target = (until.hash or "").strip().lower()
            if target and region_hash(frame, region) == target:
                return StopDecision("hash_match")
            return None
        if on == "region_hash_neq":
            target = (until.hash or "").strip().lower()
            if target and region_hash(frame, region) != target:
                return StopDecision("hash_mismatch")
            return None
        if on == "classifier":
            return self._eval_classifier(frame, until)
        if on == "luma_jump":
            return self._eval_luma_jump(frame)
        if on == "blocked":
            return self._eval_blocked(frame, as_until=True)
        if on == "overworld":
            return self._eval_overworld(frame, until)
        return None

    def _eval_classifier(self, frame: np.ndarray, until: Any) -> StopDecision | None:
        name = until.classifier
        if not name:
            return None
        present = bool(self._classify(frame).get(name))
        polarity = until.classifier_polarity
        public = _CLASSIFIER_PUBLIC.get(name, name)
        if polarity == "appears":
            if present:
                self._classifier_seen_true = True
                return StopDecision("classifier", detail=public)
            return None
        # disappears: fire when False after seeing True, or if baseline was already False.
        if present:
            self._classifier_seen_true = True
            return None
        if self._classifier_seen_true or self._disappear_met_at_baseline:
            return StopDecision("classifier", detail=public)
        return None


@dataclass
class _Shot:
    frame_index: int
    frame: np.ndarray
    interrupt: bool
    final: bool
    step_index: int | None = None


def _even_indices(count: int, cap: int) -> list[int]:
    if count <= cap:
        return list(range(count))
    if cap <= 1:
        return [count - 1]
    chosen: list[int] = []
    seen: set[int] = set()
    for i in range(cap):
        index = int(round(i * (count - 1) / (cap - 1)))
        if index not in seen:
            seen.add(index)
            chosen.append(index)
    return chosen


def _keyframe_targets(advanced: int) -> list[int]:
    n = max(1, int(advanced))
    return [max(1, int(round(n * frac))) for frac in _KEYFRAME_FRACS]


def _closest_shot(candidates: list[_Shot], target: int) -> _Shot:
    return min(candidates, key=lambda shot: (abs(shot.frame_index - target), shot.frame_index))


class ScreenshotPlan:
    def __init__(self, play: Any = None) -> None:
        self._play = play
        self._records: list[_Shot] = []
        self.final_frame: np.ndarray | None = None
        self.interrupt_frame_index: int | None = None

    def want_render(self, frame_index: int, planned: int) -> bool:
        mode = str(getattr(self._play, "screenshot_mode", "final") or "final")
        if mode != "keyframes":
            return False
        if planned <= 0:
            return False
        checkpoints = {max(1, int(round(planned * frac))) for frac in _KEYFRAME_FRACS}
        return frame_index in checkpoints

    def record(
        self,
        frame_index: int,
        frame: Any,
        *,
        interrupt: bool = False,
        final: bool = False,
        step_index: int | None = None,
    ) -> None:
        rgb = np.array(_as_rgb(frame), copy=True, order="C")
        shot = _Shot(
            frame_index=int(frame_index),
            frame=rgb,
            interrupt=bool(interrupt),
            final=bool(final),
            step_index=step_index,
        )
        self._records.append(shot)
        self.final_frame = rgb
        if shot.interrupt:
            self.interrupt_frame_index = shot.frame_index

    def package(self, play: Any) -> dict[str, Any]:
        scale = int(getattr(play, "screenshot_scale", DEFAULT_SCREENSHOT_SCALE) or DEFAULT_SCREENSHOT_SCALE)
        mode = str(getattr(play, "screenshot_mode", "final") or "final")
        regions = getattr(play, "hash_regions", None) or dict(DEFAULT_HASH_REGIONS)
        selected, subsampled = self._select(mode)
        pngs: list[bytes] = []
        pngs_native: list[bytes] = []
        screenshots: list[dict[str, Any]] = []
        for shot, kind in selected:
            native_png = encode_png(scale_nearest(shot.frame, 1))
            pngs_native.append(native_png)
            if scale == 1:
                pngs.append(native_png)
            else:
                pngs.append(encode_png(scale_nearest(shot.frame, scale)))
            entry: dict[str, Any] = {"kind": kind, "frame_index": shot.frame_index}
            if shot.step_index is not None:
                entry["step_index"] = shot.step_index
            screenshots.append(entry)
        final_native = self._final_native(selected)
        if final_native is None:
            hashes: dict[str, str] = {}
            flags = {
                "textbox_likely": False,
                "battle_likely": False,
                "start_menu_likely": False,
                "window_occluded_likely": False,
            }
        else:
            hashes = hash_named_regions(final_native, regions)
            flags = classify(final_native)
        return {
            "pngs": pngs,
            "pngs_native": pngs_native,
            "screenshots": screenshots,
            "screenshot_count": len(pngs),
            "screenshots_subsampled": subsampled,
            "screenshot_mode": mode,
            "screenshot_scale": scale,
            "native_size": [NATIVE_WIDTH, NATIVE_HEIGHT],
            "region_hashes": hashes,
            "classifiers": flags,
        }

    def _final_shot(self) -> _Shot | None:
        if not self._records:
            return None
        marked = [shot for shot in self._records if shot.final]
        return marked[-1] if marked else self._records[-1]

    def _interrupt_shot(self) -> _Shot | None:
        marked = [shot for shot in self._records if shot.interrupt]
        return marked[0] if marked else None

    def _final_native(self, selected: list[tuple[_Shot, str]]) -> np.ndarray | None:
        for shot, kind in reversed(selected):
            if kind in {"final", "interrupt_and_final"}:
                return shot.frame
        if selected:
            return selected[-1][0].frame
        shot = self._final_shot()
        return None if shot is None else shot.frame

    def _select(self, mode: str) -> tuple[list[tuple[_Shot, str]], bool]:
        if not self._records:
            return [], False
        if mode == "interrupt_and_final":
            return self._select_interrupt_and_final(), False
        if mode == "keyframes":
            return self._select_keyframes(), False
        if mode == "all":
            return self._select_all()
        shot = self._final_shot()
        assert shot is not None
        return [(shot, "final")], False

    def _select_interrupt_and_final(self) -> list[tuple[_Shot, str]]:
        interrupt = self._interrupt_shot()
        final = self._final_shot()
        if interrupt is None and final is None:
            return []
        if interrupt is None:
            assert final is not None
            return [(final, "final")]
        if final is None or interrupt.frame_index == final.frame_index:
            return [(interrupt, "interrupt_and_final")]
        pair = [(interrupt, "interrupt"), (final, "final")]
        pair.sort(key=lambda item: item[0].frame_index)
        return pair

    def _select_keyframes(self) -> list[tuple[_Shot, str]]:
        advanced = max(shot.frame_index for shot in self._records)
        remaining = list(self._records)
        picked: list[_Shot] = []
        used: set[int] = set()
        for target in _keyframe_targets(advanced):
            if not remaining:
                break
            shot = _closest_shot(remaining, target)
            if shot.frame_index in used:
                remaining = [item for item in remaining if item is not shot]
                if not remaining:
                    break
                shot = _closest_shot(remaining, target)
            picked.append(shot)
            used.add(shot.frame_index)
            remaining = [item for item in remaining if item.frame_index not in used]
        interrupt = self._interrupt_shot()
        if interrupt is not None and interrupt.frame_index not in used:
            picked.append(interrupt)
            used.add(interrupt.frame_index)
        picked.sort(key=lambda shot: shot.frame_index)
        if len(picked) > 5:
            interrupt_kept = [shot for shot in picked if shot.interrupt]
            others = [shot for shot in picked if not shot.interrupt][: 5 - len(interrupt_kept[:1])]
            picked = others + interrupt_kept[:1]
            picked.sort(key=lambda shot: shot.frame_index)
        last_index = max(shot.frame_index for shot in picked) if picked else advanced
        labeled: list[tuple[_Shot, str]] = []
        for shot in picked:
            if shot.interrupt and shot.frame_index != last_index:
                labeled.append((shot, "interrupt"))
            elif shot.interrupt and shot.final:
                labeled.append((shot, "interrupt_and_final"))
            elif shot.frame_index == last_index:
                labeled.append((shot, "final" if not shot.interrupt else "interrupt_and_final"))
            else:
                labeled.append((shot, "keyframe"))
        return labeled

    def _select_all(self) -> tuple[list[tuple[_Shot, str]], bool]:
        records = self._records
        subsampled = len(records) > MAX_SCREENSHOT_ALL
        if subsampled:
            records = [records[i] for i in _even_indices(len(records), MAX_SCREENSHOT_ALL)]
        last_index = records[-1].frame_index
        labeled: list[tuple[_Shot, str]] = []
        for shot in records:
            if shot.interrupt and shot.final:
                kind = "interrupt_and_final"
            elif shot.interrupt:
                kind = "interrupt"
            elif shot.final or shot.frame_index == last_index:
                kind = "final"
            else:
                kind = "step"
            labeled.append((shot, kind))
        return labeled, subsampled
