import asyncio  # Preserve concurrent Discord lookup exception behavior.
from collections.abc import Iterable
import re
from typing import Literal

from interactions import Guild, Member, User


async def fetch_members(guild: Guild, user_ids: Iterable[str]) -> dict[str, Member | None]:
    users = list(user_ids)
    members = await asyncio.gather(*(guild.fetch_member(user) for user in users))
    return dict(zip(users, members))


async def member_names(guild: Guild, user_ids: Iterable[str]) -> dict[str, str]:
    """Resolve non-pinging player links concurrently in the publication's guild."""
    return {user: mention_user(member, user) for user, member in (await fetch_members(guild, user_ids)).items()}


def member_usernames(members: dict[str, Member | None]) -> dict[str, str]:
    return {user: username for user, member in members.items()
            if isinstance(username := getattr(member, "username", None), str) and username}


def mention_user(user: User | Member | None, user_id: int | str = 0,
                 mention_type: Literal["Player", "Builder", "Unknown Builder", "Helper"] = "Player",
                 *, include_wishlist_tab: bool = True, display_name: str | None = None) -> str:
    if display_name is not None:
        display_name = re.sub(r"([\\`*_~\[\]<>])", r"\\\1", display_name)
    if user:
        user_id = user.id
        if display_name is None:
            display_name = str(user.display_name)
    if display_name is None:
        if mention_type == "Player":
            display_name = "❌Invalid Player"
        elif mention_type == "Builder":
            display_name = "❓Unknown Builder"
        else:
            display_name = "❌Invalid Helper"

    query = "?tab=wishlist" if include_wishlist_tab else ""
    return f"**[{display_name}](<https://discordapp.com/users/{user_id}{query}>)**"
