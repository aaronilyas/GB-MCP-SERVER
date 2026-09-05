# Historical implementation contract: screenshot-only play loop

Archived coordination record for the play-loop work that landed on `main` in
`571331c` (2026-09-04). The living public contract is README.md together with
`gb_mcp.emulator.play_limits` and `gb_mcp.emulator.input_schema`.

## Non-negotiable

1. **No game state in tool responses.** No WRAM/HRAM dumps, map IDs, X/Y, party
   lists, battle structs, PyBoy memory peeks, symbol lookup, tile-grid wrappers.
   Battery save and `pyboy.tick` may use memory internally; that data must not
   appear in JSON or images besides the LCD PNG.
2. **Screenshots stay.** Every call that advances the emulator returns PNG(s).
3. Screenshot-derived signals are allowed: pixel deltas, region hashes, coarse
   visual classifiers, optional OCR **from the PNG**, integer nearest-neighbor
   upscale. These are framebuffer features, not game state.
4. Isolated ROM validation (Nintendo logo + header checksum in a no-network
   container) is unchanged.
5. Per-user isolation (email + 32-character subdirectory) is unchanged.
6. Valid buttons: `a`, `b`, `start`, `select`, `up`, `down`, `left`, `right`.

Forbidden response-key needles live in
`gb_mcp.emulator.play_limits.FORBIDDEN_RESPONSE_KEY_NEEDLES`. CI must fail if
any appear.

## Frozen defaults (lead)

| Knob | Value | Notes |
| --- | --- | --- |
| `emulation_speed` | `0` (uncapped) | **Breaking.** Previous session start forced `set_emulation_speed(1)`. `0` / `"uncapped"` → `pyboy.set_emulation_speed(0)`. Allowed: `0,1,2,4,8`. |
| `screenshot_scale` | `4` | Nearest-neighbor only. Allowed `1,2,3,4`. Native 160×144 → 640×576. |
| `screenshot_mode` | `final` | Also `interrupt_and_final`, `keyframes`, `all` (cap 30, subsample). |
| idle timeout | **2700 s (45 min)** | **Breaking.** Was 300 s. Env `GB_PYBOY_IDLE_TIMEOUT_SECONDS` still overrides. |
| `until_eval_interval` | `4` | Range 1–15. |
| `until.threshold` | `0.08` | Fraction of changed pixels. |
| default hold abort | **on for `macro=hold`** | Full-screen `pixel_delta_above` vs **start-of-call** frame, threshold **0.12**. Disable with `disable_default_hold_abort=true` or `until.on="none"`. |
| `pixel_delta_*` baseline | start-of-call native frame | Not previous-eval-frame. |
| region hash | blake2s of native RGB bytes, digest_size=8, hex | Computed on the native 160×144 crop, never the upscaled PNG. |
| `max_frames` / total ticks | 3600 per call | Reject over-budget scripts; do not run away. |
| `steps.length` | ≤ 500 | Was 30. |
| `hold_frames` | 1–3600 | Was 1–120. |
| `gap_frames` | 0–60, default 0 | After each chord: release, then tick with no buttons. |
| call wall-clock | 20 s uncapped | Engine returns `stop_reason=call_timeout`. At speed ≥ 1, timeout is `clamp(20, frames/(60*speed)+5, 70)`. Command.wait is 25 s so the engine can answer. |
| mash | `a` 4 press / 4 release | |
| OCR | `ocr=false` | Missing engine → `ocr_text: null`, `ocr_error: "disabled"`. Never fail the input call. |

## Public request (`send_pyboy_input`)

Keep `email` + `subdirectory`. One mode per call (same rule as today: do not
pass a non-empty `buttons` list together with a non-empty `steps` list).

```json
{
  "email": "user@example.com",
  "subdirectory": "0123456789abcdef0123456789abcdef",
  "macro": "hold | mash | steps | buttons",
  "buttons": ["up"],
  "hold_frames": 3600,
  "steps": [{"buttons": ["a"], "hold_frames": 8, "gap_frames": 2, "wait": false}],
  "wait": false,
  "mash_button": "a",
  "mash_press_frames": 4,
  "mash_release_frames": 4,
  "max_frames": 3600,
  "gap_frames": 2,
  "emulation_speed": 0,
  "screenshot_mode": "final | interrupt_and_final | keyframes | all",
  "screenshot_scale": 4,
  "until": {
    "region": [0, 0, 160, 144],
    "on": "pixel_delta_above | pixel_delta_below | stable | region_hash_eq | region_hash_neq | classifier | none",
    "threshold": 0.08,
    "stable_frames": 12,
    "hash": "hex",
    "classifier": "textbox_likely | battle_likely | start_menu_likely",
    "classifier_polarity": "appears | disappears"
  },
  "until_eval_interval": 4,
  "disable_default_hold_abort": false,
  "hash_regions": {"custom": [10, 10, 20, 20]},
  "ocr": false
}
```

Mode inference (see `gb_mcp.emulator.input_schema.parse_play_input`):

- non-empty `steps` → `macro=steps` (empty `buttons: []` or `wait: true` is a wait step).
- `macro=hold` → keep `buttons` pressed until `until` / default abort / `max_frames`.
- `macro=mash` → cycle `mash_button` for `max_frames`.
- top-level non-empty `buttons` → single chord (`macro=buttons`). Old callers.
- `wait=true` (top-level) → one wait step of `hold_frames`.
- top-level `buttons=[]` **without** `wait`/`macro` is still an error (“at least one button is required”).

## Public response (status dict + PNG images)

MCP return stays `[status_dict, *Image(png)]`. `pngs` is popped before return.

```json
{
  "sent": true,
  "stop_reason": "completed | screen_change | stable | hash_match | hash_mismatch | classifier | max_frames | default_hold_abort | call_timeout | idle_timeout",
  "frames_advanced": 0,
  "emulation_speed": 0,
  "until_fired": false,
  "region_hashes": {"full": "...", "bottom": "...", "center": "..."},
  "classifiers": {
    "textbox_likely": false,
    "battle_likely": false,
    "start_menu_likely": false
  },
  "screenshot_scale": 4,
  "native_size": [160, 144],
  "screenshot_mode": "final",
  "screenshot_count": 1,
  "screenshots": [{"kind": "final", "frame_index": 0, "step_index": 0}],
  "screenshots_subsampled": false,
  "interrupt_frame_index": null,
  "default_hold_abort_applied": false,
  "macro": "buttons"
}
```

`stop_reason` mapping:

| Condition | `stop_reason` | `until_fired` |
| --- | --- | --- |
| ran to end of script / max_frames without interrupt | `completed` if script done, else `max_frames` | false |
| `pixel_delta_above` / `_below` | `screen_change` | true |
| `stable` | `stable` | true |
| `region_hash_eq` | `hash_match` | true |
| `region_hash_neq` | `hash_mismatch` | true |
| `classifier` | `classifier` | true |
| default hold abort | `default_hold_abort` | true |
| wall-clock | `call_timeout` | false |

If the caller `until` and the default hold abort fire on the same eval frame,
**prefer the caller's until reason**.

## Engine rules (sub-agent A)

- `pyboy.set_emulation_speed(emulation_speed)` at the start of the call.
- Use `button_press` / `button_release`, **not** auto-release-after-1-tick `button()` for holds.
- Intermediate frames: `pyboy.tick(n, render=False)`. Render only on capture / until-eval frames: `tick(1, render=True)` (or `tick(n, render=True)` which renders the last frame of the batch).
- Chunk ticks to the next button-state change **or** the next until-eval boundary.
- Release all buttons between chords unless the next step lists the same button and the hold is continuing.
- Wait steps tick with all buttons released.
- After every call (success, until, timeout, error) all buttons are released. `try/finally`.
- Evaluate `until` every `until_eval_interval` frames (and on the last frame).
- On interrupt: **immediately** release all buttons, capture that frame, stop.
- Do not exceed 3600 ticks. Wall-clock: if `time.monotonic()` exceeds start+timeout, stop with `call_timeout`.
- `frames_advanced` = number of emulator frames actually ticked this call.
- Return the speed actually used.

Idle loop (session thread) **must not** call `pyboy.tick()` while waiting for
commands. Uncapped speed would otherwise fast-forward the game during think
time. Wait on the command Event with a timeout equal to remaining idle.

## Vision rules (sub-agent B)

Native buffer: 160×144 RGB `uint8`. PyBoy `screen.ndarray` is RGBA — drop alpha.

**pixel_delta:** fraction of pixels in `region` whose RGB differs from the
**start-of-call** crop by any channel delta > 8 (ignore encoder noise). Divide
changed count by region pixel count.

**stable:** stop when pixel_delta vs **previous eval frame** (not start) is
`< threshold` for `stable_frames` consecutive **evaluated** frames.

**region hash:** `hashlib.blake2s(contiguous RGB bytes of the crop, digest_size=8).hexdigest()`.

**Classifiers** (coarse, prefer false positives over missing a wild battle):

- `textbox_likely`: bottom ~48px (`y >= 96`) looks like a Gen 1 dialogue box:
  dark/high-contrast rectangular frame with a lighter inner window.
- `battle_likely`: large non-overworld layout — two status-ish bars and/or a
  clear upper-enemy / lower-player split. Over-triggering is OK. Missing a
  wild-encounter takeover is not. Hold macros also have full-screen delta abort.
- `start_menu_likely`: vertical left-hand light/white menu pane (~left 80px
  mostly very light, rest of the screen different).

**Screenshot packaging:**

- `final` — one PNG after the call.
- `interrupt_and_final` — PNG at until/default-abort fire **and** final. If the
  same frame, return one image and set a screenshots entry that says so
  (`kind: "interrupt_and_final"`).
- `keyframes` — 4 images at 25/50/75/100% of frames actually advanced, plus
  interrupt frame if any and it is not already one of those four. ≤5 images.
- `all` — cap 30. If more capture points exist, subsample evenly and set
  `screenshots_subsampled: true`.
- Always encode the **upscaled** PNG (nearest-neighbor, scale default 4).
  `native_size` remains `[160, 144]`. Returned PNG width is `160 * screenshot_scale`.

B exposes callbacks the engine calls; B does **not** schedule buttons.

Suggested surface:

```python
# gb_mcp/emulator/vision.py
capture_native(pyboy) -> np.ndarray  # (144, 160, 3) uint8
scale_nearest(frame, scale) -> PIL.Image
encode_png(image) -> bytes
region_hash(frame, box) -> str
pixel_delta_fraction(a, b, region) -> float
classify(frame) -> dict[str, bool]
hash_named_regions(frame, regions) -> dict[str, str]

class UntilMonitor:
    def __init__(self, play_input, baseline_frame)
    def evaluate(self, frame, eval_index) -> StopDecision | None

class ScreenshotPlan:
    def want_render(self, frame_index, planned) -> bool
    def record(self, frame_index, frame, *, interrupt=False, final=False)
    def package(self, play_input) -> {pngs, screenshots, screenshot_count, subsampled}
```

## Session rules (sub-agent C)

- Default idle 45 minutes (`play_limits.DEFAULT_IDLE_TIMEOUT_SECONDS` and
  `config.IDLE_TIMEOUT_SECONDS`).
- `ping` command: reset `_last_input_at`, **do not tick**, **do not press**.
  Return `{alive: true, idle_timeout_seconds, seconds_since_last_input, ...}`.
- `save` command: write cartridge save state, leave PyBoy running.
  Return `{saved: true, ...}`.
- `stop` and idle auto-stop still save then close.
- `load_subdirectory_rom` accepts `emulation_speed` and `idle_timeout_seconds`.
- `submit_gb_rom` already maps when `email` is present. Add `boot=true`: after
  map, start PyBoy and merge `load_subdirectory_rom` fields into the result.
- Docker instance: add `POST /ping` and `POST /save`. `POST /input` must pass
  the full play payload (not only `steps` + `screenshot_mode`). Idle env
  default 2700. Optional `GB_PYBOY_EMULATION_SPEED`.
- `PlaySession.submit` must accept ops other than `"input"` (`ping`, `save`,
  `stop` already exists on the loop).
- Idle wait: `Event.wait(timeout=remaining)` — **no tick**.

## New tools

- `ping_pyboy(email, subdirectory)`
- `save_battery(email, subdirectory)`

Keep existing tool names working with backward-compatible defaults except the
documented speed/idle/scale breaks.

## File ownership

| Owner | May write | Must not write |
| --- | --- | --- |
| **Lead** | `docs/AGENT_CONTRACT.md`, `gb_mcp/emulator/play_limits.py`, `gb_mcp/emulator/input_schema.py`, final merge of overlapping files, `IMPLEMENTATION_REPORT.md`, default-hold-abort decision | — |
| **A — input + speed** | `gb_mcp/emulator/input_engine.py` (**new**), `tests/test_input_engine.py` (**new**) | `vision.py`, `server.py`, MCP schema text, idle timeout numbers, classifiers |
| **B — framebuffer** | `gb_mcp/emulator/vision.py` (**new**), `tests/test_vision.py` (**new**), optional `tests/fixtures/lcd/*.png` | button scheduling, session timeout, `server.py` |
| **C — session** | `gb_mcp/config.py` (idle default), `gb_mcp/emulator/loop.py` (**ping/save/idle-wait/speed-on-start only** — do not rewrite `_apply_input`), `gb_mcp/emulator/session.py`, `gb_mcp/emulator/backend.py`, `gb_mcp/emulator/instance.py`, `docker/instance_server.py`, `tests/test_session_lifecycle.py` (**new**) | `vision.py`, `input_engine.py`, framebuffer math |
| **D — schemas/docs** | `server.py` tool signatures/descriptions, `_USAGE_GUIDE`, `README.md`, `.env.example`, `tests/test_resources.py` tool-name list | engine internals |
| **E — tests** | `tests/test_play_loop.py`, updates to `tests/test_server.py` / `tests/test_emulator.py` / `tests/conftest.py` as needed | production logic except tiny test doubles |
| **F — OCR** | `gb_mcp/emulator/ocr.py` (**new**), optional extra in `requirements-instance.txt` | default path of input |

Lead wires `_apply_input` in `loop.py` after A and B land:

```python
from gb_mcp.emulator.input_schema import parse_play_input
from gb_mcp.emulator.input_engine import run_play_input
from gb_mcp.emulator.vision import capture_native, UntilMonitor, ScreenshotPlan, ...
```

## Input engine API (A)

```python
def run_play_input(
    pyboy,
    play: PlayInput,
    *,
    capture_native,          # callable() -> HxWx3 uint8
    until_monitor,           # object with evaluate(frame, eval_index) -> StopDecision|None
    screenshot_plan,         # object with want_render / record / package
    monotonic=time.monotonic,
) -> dict:
    """Drive buttons and ticks. Return engine fields + screenshot_plan.package()."""
```

`StopDecision` is a simple namespace: `reason: str` (one of STOP_REASONS),
`until_fired: bool`.

A may define a tiny Protocol; do not import vision types if that creates a
cycle — accept duck-typed objects.

## Default hold abort (lead decision, B implements)

When `play.apply_default_hold_abort` is true (`macro=hold` and not disabled):

On every until-eval (including interval 4), compute full-screen pixel_delta vs
the start-of-call frame. If `> 0.12`, stop with `default_hold_abort` unless the
caller `until` also fires on this same frame (caller wins).

This is **in addition to** a caller-supplied `until`.

## What is explicitly not added

- Memory-read tools, `get_game_state`, symbol maps, party parsers, map IDs.
- Pokémon ROM in CI. Use FakePyBoy / tiny valid GB ROM / synthetic 160×144 frames.
- Rewriting ROM validation or the 32-character subdirectory scheme.
