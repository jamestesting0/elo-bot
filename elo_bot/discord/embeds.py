import interactions as ipy
from pydantic import JsonValue

from elo_bot.config.presentation import FOOTER_LOGOS
from elo_bot.discord import runtime


def footer_image() -> str:
    return FOOTER_LOGOS[getattr(runtime, "VERSION", "main")]


def embed_edit_payload(embed: ipy.Embed) -> dict[str, JsonValue] | list[dict[str, JsonValue]]:
    """Include an explicit description reset; Embed.to_dict drops None and empty strings."""
    payload = embed.to_dict()
    match payload:
        case list():
            payload[0]["description"] = embed.description or ""
        case dict():
            payload["description"] = embed.description or ""
    return payload
