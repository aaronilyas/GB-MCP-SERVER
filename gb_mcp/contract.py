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
Walk: omit frames on a single D-pad to hold-walk (default 240); pass frames=16 to tap one tile or line up.
Holds abort on combat HUD, text, menu, fade, or blocked. After boot logos use intent=skip_intro.
Dialogue: mash A or intent=advance_text until the box is gone. Mash pulses A; do not hold A.
If looks_like=battle, use intent=run_away or intent=battle_turn. Do not open START on a fight HUD.
After a door, wait fade-disappears or until=overworld — not stable on black. intent=enter_door if already facing in.
If stopped_reason=blocked, change direction. A short tap may only turn facing; that is not blocked.
On long walks, look at the GIF or the last screenshot (end state), not an early frame.
until: battle|textbox|menu|stable|fade|blocked|overworld. until_polarity appears|disappears (or until=textbox_end).
One live session per user. save snapshots; stop and idle auto-save then close.
Large ROM dumps use HTTP POST /roms, not chat chunks. Small homebrew may use add_rom with rom_base64.
"""
