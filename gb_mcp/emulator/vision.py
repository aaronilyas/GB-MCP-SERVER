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
    DEFAULT_HASH_REGIONS,
    DEFAULT_REGION,
    DEFAULT_SCREENSHOT_SCALE,
    MAX_SCREENSHOT_ALL,
    NATIVE_HEIGHT,
    NATIVE_WIDTH,
    SCREENSHOT_SCALES,
)

# Channel delta ignored as encoder/LCD noise when comparing frames.
_PIXEL_DELTA_TOLERANCE = 8
_KEYFRAME_FRACS = (0.25, 0.50, 0.75, 1.0)
_PNG_FORMAT = "PNG"

# Classifier thresholds are coarse (synthetic 160x144 fixtures, not ROM dumps).
_TEXTBOX_Y0 = 96
_TEXTBOX_BORDER_MAX = 80.0
_TEXTBOX_INNER_MIN = 180.0
_TEXTBOX_CONTRAST_MIN = 80.0
_BATTLE_SPLIT_MIN = 25.0
_BAR_ROW_LUM_MIN = 190.0
_BAR_MIN_WIDTH_FRAC = 0.35
_BAR_THICKNESS = (2, 10)
_MENU_X1 = 80
_MENU_LIGHT_MIN = 200.0
_MENU_LIGHT_FRAC = 0.55
_MENU_HEIGHT_FRAC = 0.70
_MENU_ROW_LIGHT_FRAC = 0.60
_MENU_PANE_DELTA = 40.0


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
    """Return the current LCD as contiguous (144, 160, 3) uint8 RGB."""
    screen = getattr(pyboy, "screen", None)
    if screen is None:
        raise RuntimeError("PyBoy screen is unavailable")
    frame: np.ndarray | None = None
    raw = getattr(screen, "ndarray", None)
    if raw is not None:
        try:
            candidate = np.asarray(raw)
            if getattr(candidate, "ndim", 0) >= 2:
                frame = candidate
        except Exception:
            frame = None
    if frame is None:
        image = getattr(screen, "image", None)
        if image is None:
            raise RuntimeError("PyBoy screen image is unavailable")
        frame = np.asarray(image)
    rgb = np.array(_as_rgb(frame), dtype=np.uint8, copy=True, order="C")
    return rgb


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


def _textbox_likely(rgb: np.ndarray) -> bool:
    """Gen 1-style dialogue: dark bottom frame, much lighter inner window."""
    if rgb.shape[0] < NATIVE_HEIGHT or rgb.shape[1] < NATIVE_WIDTH:
        return False
    bottom = rgb[_TEXTBOX_Y0:, :, :]
    lum = _luminance(bottom)
    inner = lum[6:42, 8:152]
    if inner.size == 0:
        return False
    border = np.concatenate(
        (lum[:4, :].ravel(), lum[-4:, :].ravel(), lum[:, :4].ravel(), lum[:, -4:].ravel())
    )
    border_mean = float(border.mean())
    inner_mean = float(inner.mean())
    return (
        border_mean < _TEXTBOX_BORDER_MAX
        and inner_mean > _TEXTBOX_INNER_MIN
        and (inner_mean - border_mean) > _TEXTBOX_CONTRAST_MIN
    )


def _light_horizontal_strips(lum: np.ndarray) -> list[tuple[int, int]]:
    """Thin bright status-bar-like row runs (height 2–10)."""
    if lum.size == 0:
        return []
    row_frac = (lum >= _BAR_ROW_LUM_MIN).mean(axis=1)
    is_bar = row_frac >= _BAR_MIN_WIDTH_FRAC
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


def _battle_likely(rgb: np.ndarray) -> bool:
    """Split upper/lower layout and/or two light status bars. Over-trigger OK."""
    lum = _luminance(rgb)
    height = lum.shape[0]
    third = max(1, height // 3)
    top_mean = rgb[:third].reshape(-1, 3).mean(axis=0)
    bot_mean = rgb[-third:].reshape(-1, 3).mean(axis=0)
    split = float(np.abs(top_mean - bot_mean).mean())
    strips = _light_horizontal_strips(lum)
    if split > _BATTLE_SPLIT_MIN and len(strips) >= 1:
        return True
    return len(strips) >= 2


def _start_menu_likely(rgb: np.ndarray) -> bool:
    """Vertical left-hand light pane covering most of the height; rest different."""
    left = rgb[:, :_MENU_X1]
    right = rgb[:, _MENU_X1:]
    left_lum = _luminance(left)
    right_lum = _luminance(right)
    light = left_lum >= _MENU_LIGHT_MIN
    if float(light.mean()) < _MENU_LIGHT_FRAC:
        return False
    row_frac = light.mean(axis=1)
    if float((row_frac >= _MENU_ROW_LIGHT_FRAC).mean()) < _MENU_HEIGHT_FRAC:
        return False
    if abs(float(left_lum.mean()) - float(right_lum.mean())) < _MENU_PANE_DELTA:
        return False
    return True


def classify(frame: Any) -> dict[str, bool]:
    rgb = _as_rgb(frame)
    return {
        "textbox_likely": _textbox_likely(rgb),
        "battle_likely": _battle_likely(rgb),
        "start_menu_likely": _start_menu_likely(rgb),
    }


class StopDecision:
    def __init__(self, reason: str, until_fired: bool = True) -> None:
        self.reason = reason
        self.until_fired = until_fired


class UntilMonitor:
    def __init__(self, play: Any, baseline_frame: Any) -> None:
        self.play = play
        self.baseline_frame = np.array(_as_rgb(baseline_frame), copy=True, order="C")
        self._prev_eval_frame: np.ndarray | None = None
        self._stable_streak = 0
        self._classifier_seen_true = False
        self._disappear_met_at_baseline = False
        until = getattr(play, "until", None)
        classifier = getattr(until, "classifier", None) if until is not None else None
        if until is not None and until.on == "classifier" and classifier:
            present = bool(classify(self.baseline_frame).get(classifier))
            self._classifier_seen_true = present
            if until.classifier_polarity == "disappears" and not present:
                self._disappear_met_at_baseline = True

    def evaluate(self, frame: Any, eval_index: int) -> StopDecision | None:
        del eval_index
        rgb = np.array(_as_rgb(frame), copy=True, order="C")
        caller = self._eval_caller_until(rgb)
        abort = self._eval_default_hold_abort(rgb)
        self._prev_eval_frame = rgb
        if caller is not None:
            return caller
        return abort

    def _eval_default_hold_abort(self, frame: np.ndarray) -> StopDecision | None:
        if not getattr(self.play, "apply_default_hold_abort", False):
            return None
        threshold = float(
            getattr(self.play, "default_hold_abort_threshold", 0.12)
        )
        delta = pixel_delta_fraction(self.baseline_frame, frame, DEFAULT_REGION)
        if delta > threshold:
            return StopDecision("default_hold_abort", True)
        return None

    def _eval_caller_until(self, frame: np.ndarray) -> StopDecision | None:
        until = getattr(self.play, "until", None)
        if until is None or until.on in {"none", None}:
            return None
        region = until.region or DEFAULT_REGION
        on = until.on
        if on == "pixel_delta_above":
            if pixel_delta_fraction(self.baseline_frame, frame, region) > until.threshold:
                return StopDecision("screen_change")
            return None
        if on == "pixel_delta_below":
            if pixel_delta_fraction(self.baseline_frame, frame, region) < until.threshold:
                return StopDecision("screen_change")
            return None
        if on == "stable":
            if self._prev_eval_frame is None:
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
        return None

    def _eval_classifier(self, frame: np.ndarray, until: Any) -> StopDecision | None:
        name = until.classifier
        if not name:
            return None
        present = bool(classify(frame).get(name))
        polarity = until.classifier_polarity
        if polarity == "appears":
            if present:
                self._classifier_seen_true = True
                return StopDecision("classifier")
            return None
        # disappears: fire when False after seeing True, or if baseline was already False.
        if present:
            self._classifier_seen_true = True
            return None
        if self._classifier_seen_true or self._disappear_met_at_baseline:
            return StopDecision("classifier")
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
        screenshots: list[dict[str, Any]] = []
        for shot, kind in selected:
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
            }
        else:
            hashes = hash_named_regions(final_native, regions)
            flags = classify(final_native)
        return {
            "pngs": pngs,
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
