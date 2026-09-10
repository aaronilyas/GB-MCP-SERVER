# Changelog

## [Unreleased] — 2026-09-10

### Screenshot-only play speed + game-agnostic LCD interrupts

- The MCP Image is the **final** LCD of the call (4× PNG) or the action GIF — never an early keyframe. Public `screenshots` default to one native 160×144 `kind: "final"` (optional `interrupt` if an until/abort fired on another frame). Long mash/hold still pack a GIF; `media=image` does not hide it; the final native PNG remains in `screenshots`.
- Omit `frames` on a single D-pad to hold-walk (**default 240**, `macro=hold` + default abort). Explicit `frames=16` stays a one-tile tap. A/B omitted frames stay 16. Mash without frames still uses the mash cap.
- Classifiers describe **GB LCD geometry** for any uploaded ROM: framed dialogue windows (bottom or lower-center, typewriter-partial OK), combat HUDs (dual status bars and/or command pane; tilemap fields rejected), pause/title panes (left, right, or full-width), and fade/occlusion that is **new vs the start-of-call baseline** (static camera letterbox is scenery). `until=stable` / `overworld` ignore near-uniform black/white warps until texture returns.
- Hold abort: real combat/menu/textbox/fade only — camera scroll on a textured field must not look like battle. Blocked grace ≥ ~16 frames so a facing turn is not `stopped_reason=blocked`.
- Intents (still six public tools): `advance_text` waits for a box then mashes; `run_away` pulses B and only navigates a detected command pane; `battle_turn` confirms with A on classifier edges (not `until=stable`); new `skip_intro` and `enter_door`. No game-specific menu cell graph as the only path.
- OCR crops the `textbox_likely` inner rect from the PNG. `HOW_TO_PLAY` stays ≤2000 chars and game-agnostic. History under `docs/history/` is frozen, not living API.

### Screenshot-only play: GIF default, mash pulses, honest looks_like, wall abort

- Long public `play` mash and directional holds (`frames` ≥ 30) return one looping GIF (GIF89a, 1–3s) plus a native 160×144 final PNG. Short taps stay a single PNG. `media=video` is accepted; `media=image` does not suppress the GIF. Do not dump a PNG keyframe list as the default observation.
- Public `play(buttons=["up"], frames=240)` is a directional **hold** with default abort (battle, text, menu, fade, blocked wall). A 16-frame tap stays a one-tile chord.
- Public mash (`play(mash=true)` / `intent=advance_text`) pulses A (12 press / 8 release), stops when the textbox disappears, releases all buttons, then waits a short released gap so the next A does not re-talk. No textbox at start → brief wait, not hundreds of held-A frames.
- `until_polarity=appears|disappears` (default appears). Aliases: `textbox_end` / `clear_text` (textbox disappears), `overworld` (battle gone then stable). Public `until=fade` is a luma jump vs start-of-call, not full-screen camera scroll. `until=blocked` is a coarse center crop (`PLAYER_BLOCKED_REGION` + `BLOCKED_COARSE_L1`) that stays still (walk-cycle bob and off-center NPCs ignored).
- `looks_like` stays LCD-only and is omitted when no classifier is confidently true. Textured overworld / house facades are not `battle` or `menu`. Fade is not a dark rug. `looks_like` prefers textbox over battle. Public JSON may include `stopped_reason`, `player_moved`, `textbox_complete`, and `ocr_text` (inner textbox crop only).
- Public directional holds abort with `stopped_reason=blocked` / `player_moved=false` when the coarse center crop is stuck. Camera scroll still completes. Combat / menu / textbox / fade still abort first.
- Gap cap is 180 frames. Optional `intent`: `advance_text`, `run_away`, `battle_turn` — composed from existing engine primitives, not new MCP tools. `skip_intro` and `enter_door` landed in the section above.
- `HOW_TO_PLAY` teaches long holds, pulse mash, flee/turn intents, and reading the GIF on long walks. Catalog tests pin that text and the published `play` schema so a stale hosted deploy cannot silently serve the pre-GIF catalog.

## [Unreleased] — 2026-09-08

### Consent email and optional tool email identity

- OAuth consent collects an email. Successful access tokens carry that address as `email` plus an email-shaped `sub`. Success no longer issues `gb-mcp-user`.
- Optional `email` is restored on `list_games`, `boot`, and `add_rom`. Explicit `email` wins over token identity. `play` / `save` / `stop` stay session-bound and take no email argument.
- `tools/list` publishes `email` as an optional string (same description as `_EMAIL_DESCRIPTION`) on those three tools and sets `additionalProperties: true` so a hosted catalog cannot drop an explicit email. Extra `email` still reaches `require_email(explicit=email)`. `play` / `save` / `stop` stay email-less.
- `POST /roms` maps from the token email or an optional JSON/form `email`.
- If `email` is omitted and there is no token identity, those tools return a structured `model_request` asking for email — do not invent one (for example `trainer@x.ai`).
- The six-tool catalog is unchanged: `add_rom`, `list_games`, `boot`, `play`, `save`, `stop`.

## [Unreleased] — 2026-09-06

### Model-facing catalog shrink

- MCP `tools/list` is six tools: `add_rom`, `list_games`, `boot`, `play`, `save`, `stop`. The model no longer sees chunked upload tools, `map_subdirectory_to_email`, `ping_pyboy`, or the old play names (`submit_gb_rom`, `list_subdirectories_for_email`, `load_subdirectory_rom`, `reset_pyboy`, `send_pyboy_input`, `save_battery`, `stop_pyboy`).
- After `boot`, `play` / `save` / `stop` take no email or subdirectory. Identity comes from the OAuth session bind. Large dumps use `POST /roms`, not chat chunks.
- Play replies are `{ok, frames, stopped, game}` plus one 4× PNG or one short GIF. The model no longer sees hashes, OCR, screenshot modes, idle countdowns, or `battle_likely`.
- Resources are `gb://how-to-play`, `gb://screen`, `gb://session`. `gb://usage` and `gb://users/{email}/...` are gone. Server `instructions` match `gb://how-to-play`.

## [Unreleased] — 2026-09-05

### Play-session LCD, classifiers, hold abort, and email

- Capture copies the composited 160×144 RGB LCD (`pyboy.screen.ndarray` is a live RGBA view, not a raw BG/window layer). After `load_state`, settle is 8× `tick(1, render=True)` with buttons released — PyBoy `tick(n, render=True)` only composes the last frame of a batch, which left interiors that use the GB window with a stale black slab. Capture/until-eval frames still compose with `tick(1, render=True)`. Additive `window_occluded_likely` on `classifiers` is diagnostic only (not `until.classifier`, does not abort input).
- **Superseded** (classifiers are generic LCD geometry as of 2026-09-10; see the game-agnostic LCD interrupts section above). Historical: `battle_likely` was a Gen 1 fight-LCD heuristic: HP-bar-like strips in the enemy/player slots, rejected when `textbox_likely` or `start_menu_likely` is already true. Pallet-like overworld (tree belt vs pavement, fences/ledges) is false. Use `until.classifier=battle_likely` for grass → fight LCD takeover, not for walking, textboxes, or the Start menu.
- Default `macro=hold` abort is two-gate: full-frame `pixel_delta` > 0.12 **and** (`battle_likely` or `start_menu_likely` became true, or mean luminance jumped by > 80). Camera scroll / 1–3 tile walks do not abort; battle takeover, start menu, and warp fade do. `disable_default_hold_abort=true` and `until.on=none` still force-off.
- Instance-proxied tool replies that include `email` echo the mapped caller, never the Docker placeholder `"instance"`. The play container may still boot with `email="instance"` internally; the MCP host rewrites on the way out (`save_battery`, `ping_pyboy`, `send_pyboy_input`, `discard_state`, status).

### Snapshot vs cartridge battery

- `rom.gb.state` is a PyBoy `save_state` snapshot used to resume a session, not cartridge battery. `save_battery` writes that snapshot without stopping; it is not a substitute for `reset_pyboy`. Stop and idle still write the snapshot, then `pyboy.stop(save=True)` flushes cartridge SRAM (`rom.gb.ram`). Live `save_ram` on `save_battery` is optional. A failed `load_state` sets `restore_error`, leaves `restored_state=false`, and cold-boots. A successful restore ticks 8 frames (`tick(1, render=True)` each) with buttons released before the session is ready.
- `reset_pyboy(email, subdirectory, discard_state=true, restore_state=false)` stops the instance if any, unlinks `rom.gb.state` when `discard_state` is true, then loads again with `restore_state=false` (cold boot without the previous PyBoy snapshot). Cartridge SRAM is left alone.
- `load_subdirectory_rom` accepts `restore_state` (default `true`, same resume as today).

### Session email from OAuth

- Play and mapping tools (`list_subdirectories_for_email`, `load_subdirectory_rom`, `reset_pyboy`, `send_pyboy_input`, `ping_pyboy`, `save_battery`, `stop_pyboy`, `submit_gb_rom`, `begin_gb_rom_upload`, `finalize_gb_rom_upload`, `map_subdirectory_to_email`) accept omitted `email` when the current OAuth access token has an `email` or `sub` claim. Explicit `email` still wins. If `email` is omitted and there is no token identity, the result includes a structured `model_request` asking for email (do not invent `trainer@x.ai`). Email is not transport auth; bearer/OAuth stays in `gb_mcp/http.py`.

### Screenshot-only play loop

- `send_pyboy_input` accepts macros (`hold`, `mash`, `steps`, `buttons`), `until` framebuffer interrupts, wait steps, `gap_frames`, screenshot modes `interrupt_and_final` / `keyframes`, `screenshot_scale` 1–4 (default 4), and uncapped `emulation_speed` (default 0). Caps: 500 steps, `hold_frames` 1–3600. There is no memory or game-state tool; `until` is screenshot-derived on the native 160×144 LCD.
- Idle timeout is 45 minutes (`GB_PYBOY_IDLE_TIMEOUT_SECONDS`, default 2700). `ping_pyboy` resets the idle timer without advancing emulation.
- `load_subdirectory_rom` accepts `emulation_speed` and `idle_timeout_seconds`. `submit_gb_rom` accepts `boot=true` to start PyBoy after a mapped submit.

### Size-strict ROM validation and chunked uploads

- Isolated validator is size-strict: Nintendo logo + header checksum + playable size. A known header size code (0x0148) whose file is shorter than the expected length is rejected (truncated dumps, including a 1 KiB Pokémon header, are not persisted or booted). Extra bytes are allowed only as a whole 16 KiB bank pad. Unrecognized size codes are rejected unless `GB_ROM_ALLOW_UNKNOWN_SIZE=1`. Listing includes `playable` / `unplayable_reason`.
- `begin_gb_rom_upload` / `append_gb_rom_upload` / `append_gb_rom_upload_batch` / `finalize_gb_rom_upload` stream a ROM in connector-safe chunks (default 8 KiB decoded; override with `GB_ROM_UPLOAD_CHUNK_BYTES`), then run the same isolated validator. `append_gb_rom_upload_batch` applies up to 16 consecutive chunks (64 KiB decoded) in one call so hosted connectors do not truncate ~32 KiB base64 single-chunk args. `abort_gb_rom_upload` deletes staging. `submit_gb_rom` still works for small homebrew; 1 MiB dumps must use the chunked tools.
- `submit_gb_rom` and `finalize_gb_rom_upload` accept optional `subdirectory` plus `email` to atomically overwrite the `.gb`/`.gbc` in an owned mapping (same 32-hex id). Unmapped, other-owned, or invalid hex names are rejected and nothing is persisted. A truncated sibling is deleted so load cannot boot the 1 KiB dump. `load_subdirectory_rom` calls `assert_rom_playable` before any play instance starts and names the finalize-with-subdirectory repair path.
- Abandoned staging under `roms/.uploads/` expires after 30 minutes. `list_subdirectories_for_email` also runs that expiry so idle servers reclaim disk without a later upload.
- Play-instance boot errors include a short sanitized reason (exit code + instance JSON `error`, including truncation actual vs expected byte counts). Raw docker logs are still not returned. A truncated file never starts a container.

### Agent ingest contract

- Default decoded chunk size is 8 KiB (`GB_ROM_UPLOAD_CHUNK_BYTES`). 1 MiB Pokémon dumps: `begin_gb_rom_upload` → `append_gb_rom_upload_batch` → `finalize_gb_rom_upload(..., email, boot=true)`. Never put ~32 KiB of base64 in a single tool argument when the client is an LLM. No host-path ingest. After map/boot, call `ping_pyboy` if think time exceeds ~30 seconds. One live session per email. Play remains screenshot-only; there is no memory or game-state tool.
