import json
import os
from pathlib import Path
import re

import interactions as ipy

_EMOJI_DIR = Path(__file__).resolve().parents[2] / "assets" / "emojis"
_SOURCES = json.loads((_EMOJI_DIR / "application_sources.json").read_text(encoding="utf-8"))
_LEGACY_NAMES = {entry["legacy_id"]: name for name, entry in _SOURCES.items() if "legacy_id" in entry}
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:(\d+)>")


def _fallback_id(name: str, entry: dict) -> str | None:
    if "legacy_id" in entry:
        return entry["legacy_id"]
    value = os.getenv(f"EMOJI_{name.upper()}", "")
    return value if value.isascii() and value.isdecimal() else None


_EMOJIS = {
    name: ipy.PartialEmoji(name=name, id=_fallback_id(name, entry), animated=entry.get("animated", False))
    for name, entry in _SOURCES.items()
}
TOKEN_ITEM_EMOJIS = {
    name: _EMOJIS[name] for name, entry in _SOURCES.items() if "file" in entry and _EMOJIS[name].id
}


def application_emoji(name: str) -> ipy.PartialEmoji:
    return _EMOJIS[name]


def configure_application_emojis(version: str) -> None:
    mapping = _EMOJI_DIR / "application_ids.json"
    ids = json.loads(mapping.read_text(encoding="utf-8")).get(version, {}) if mapping.exists() else {}
    TOKEN_ITEM_EMOJIS.clear()
    for name, entry in _SOURCES.items():
        emoji = _EMOJIS[name]
        emoji.id = ids.get(name) or _fallback_id(name, entry)
        if "file" in entry and emoji.id:
            TOKEN_ITEM_EMOJIS[name] = emoji


def render_emoji_markup(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = _LEGACY_NAMES.get(match[1])
        return str(_EMOJIS[name]) if name else match[0]

    return _CUSTOM_EMOJI.sub(replace, text)


SUCCESS_EMOJI = application_emoji("success")
ERROR_EMOJI = application_emoji("error")
TOKEN_EMOJI = application_emoji("token")
STAR_EMOJI = application_emoji("cocstar")
ALEX_EMOJI = application_emoji("alex")
WIN_EMOJI = application_emoji("upkey")
LOSS_EMOJI = application_emoji("downkey")
TICK_EMOJI = application_emoji("whitetick")
CROSS_EMOJI = application_emoji("whitecross")
STAFF_EMOJI = application_emoji("stafficon")
DISTRICT_EMOJI = application_emoji("dh5")
CLAN_EMOJI = application_emoji("clan")
IMPORTANT_EMOJI = application_emoji("important")
GIVEAWAY_EMOJI = application_emoji("giveaway")
CLASHING_SWORD_EMOJI = application_emoji("swordsclashing")
LOADING_EMOJI = application_emoji("loading")
RANK_EMOJIS = ["🥇", "🥈", "🥉"]
RANK_REVERSED_EMOJIS = ["🤡", "🐷", "🌚"]
BASE_QUALITY_EMOJIS = {
    "Average": "😐",
    "Good": "🎖️",
    "Great": "🏅",
    "Excellent": "🏆",
    "Private": "🔒"
}
TOWNHALL_EMOJIS = {level: application_emoji(f"th{level}") for level in range(1, 17)}
LEAGUE_EMOJIS = {
    "Miner Spammer": application_emoji("SuperMiner"),
    "Amateur": application_emoji("golme"),
    "Battle-Tested": application_emoji("knight"),
    "Unstoppable": application_emoji("superchad"),
    "Touch Grass Please": application_emoji("touchgrass")
}
