from __future__ import annotations

from gb_mcp.contract import HOW_TO_PLAY, HOW_TO_PLAY_MAX_CHARS

_TOOLS = ("list_games", "boot", "play", "save", "stop", "add_rom")
_FORBIDDEN = (
    "blake2s",
    "battle_likely",
    "begin_gb_rom_upload",
    "ping_pyboy",
    "send_pyboy_input",
    "bearer",
)
_OLD_TOOLS = (
    "send_pyboy_input",
    "submit_gb_rom",
    "map_subdirectory_to_email",
)
_GAME_SPECIFIC_NEEDLES = (
    "pokemon",
    "pallet",
    "oak",
    "brock",
    "pkmn",
    "squirtle",
)


def test_how_to_play_is_short_nonempty_str() -> None:
    assert isinstance(HOW_TO_PLAY, str)
    text = HOW_TO_PLAY.strip()
    assert text
    assert HOW_TO_PLAY_MAX_CHARS <= 2500
    assert len(text) <= HOW_TO_PLAY_MAX_CHARS
    lines = text.splitlines()
    assert 8 <= len(lines) <= 25


def test_how_to_play_omits_forbidden_substrings() -> None:
    text = HOW_TO_PLAY.lower()
    for needle in _FORBIDDEN:
        assert needle not in text, needle


def test_how_to_play_mentions_six_tools_and_loop() -> None:
    for name in _TOOLS:
        assert name in HOW_TO_PLAY, name
    lower = HOW_TO_PLAY.lower()
    cursor = -1
    for needle in ("list_games", "boot", "play", "look"):
        pos = lower.find(needle, cursor + 1)
        assert pos >= 0, needle
        cursor = pos


def test_how_to_play_omits_old_tool_names() -> None:
    text = HOW_TO_PLAY.lower()
    for name in _OLD_TOOLS:
        assert name not in text, name


def test_how_to_play_teaches_last_image_blocked_mash_and_intent() -> None:
    text = HOW_TO_PLAY
    assert len(text.strip()) <= HOW_TO_PLAY_MAX_CHARS
    assert "blocked" in text
    assert "intent" in text
    assert "until_polarity" in text or "textbox_end" in text
    lower = text.lower()
    for needle in ("blake2s", "battle_likely", "ping_pyboy", "send_pyboy_input"):
        assert needle not in lower, needle
    for name in _TOOLS:
        assert name in text, name
    assert "omit frames" in lower
    assert "1800" in text
    assert "frames=16" in text
    assert "skip_intro" in text
    assert "enter_door" in text
    assert "advance_text" in text
    assert "battle_until_overworld" in text or "battle_turn" in text
    assert "last" in lower and "image" in lower
    assert "overworld" in lower
    assert "fade" in lower
    assert "do not request gif" in lower or "do not request gifs" in lower
    assert "diaries" in lower or "frame-by-frame" in lower
    assert "never hold a" in lower


def test_how_to_play_omits_game_specific_needles() -> None:
    lower = HOW_TO_PLAY.lower()
    for needle in _GAME_SPECIFIC_NEEDLES:
        assert needle not in lower, needle
