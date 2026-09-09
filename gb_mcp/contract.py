"""Model-facing how-to for Game Boy MCP play.

Later used as MCP ``instructions`` and ``gb://how-to-play``.
"""

from __future__ import annotations

HOW_TO_PLAY_MAX_CHARS = 2000

HOW_TO_PLAY = """\
Game Boy MCP.
Loop: list_games → boot → play → look at the returned image or video → repeat.
Tools: list_games, boot, play, save, stop, add_rom.
list_games lists your titles.
boot starts a session by title or id. Default restores the last snapshot.
reset=true cold-boots and drops the snapshot.
list_games, boot, and add_rom take optional email. Consent or token email also binds; explicit email wins.
If unbound, they ask for email — do not invent one. After boot, play / save / stop take no email.
Walk with a long directional hold (frames in the hundreds). It aborts on battle, text, menu, fade, or blocked. Do not tap 16 frames.
Dialogue: mash A or intent=advance_text until the box is gone. Do not A-spam in 8-frame steps.
If looks_like=battle, use intent=run_away or intent=battle_turn. Do not open START.
After a door, wait through fade then until=stable before walking.
If player_moved is false or stopped_reason=blocked, change direction. Do not repeat the same hold.
On long walks, look at keyframes or the GIF, not only the last frame.
until: battle|textbox|menu|stable|fade|blocked. until_polarity appears|disappears (or until=textbox_end).
One live session per user. save snapshots; stop and idle auto-save then close.
Large ROM dumps use HTTP POST /roms, not chat chunks. Small homebrew may use add_rom with rom_base64.
"""
