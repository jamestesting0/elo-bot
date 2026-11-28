import interactions as ipy
from pydantic import JsonValue

from elo_bot.config.presentation import EMBED_COLORS, FOOTER_LOGOS
from elo_bot.discord import runtime


def embed_color() -> int:
    return EMBED_COLORS[getattr(runtime, "VERSION", "main")]


def footer_image() -> str:
    user = getattr(getattr(runtime, "bot", None), "user", None)
    if isinstance(user, ipy.ClientUser):
        return user.display_avatar.as_url(size=64)
    return FOOTER_LOGOS[getattr(runtime, "VERSION", "main")]


def profile_author(user: ipy.User | ipy.Member | None, *, display_name: str | None = None) -> ipy.EmbedAuthor:
    if user is None:
        return ipy.EmbedAuthor(name=display_name or "Unknown Player")
    return ipy.EmbedAuthor(name=display_name or user.display_name, url=f"https://discord.com/users/{user.id}",
                           icon_url=user.display_avatar.url)


def embed_edit_payload(embed: ipy.Embed) -> dict[str, JsonValue] | list[dict[str, JsonValue]]:
    """Include an explicit description reset; Embed.to_dict drops None and empty strings."""
    payload = embed.to_dict()
    match payload:
        case list():
            payload[0]["description"] = embed.description or ""
        case dict():
            payload["description"] = embed.description or ""
    return payload
