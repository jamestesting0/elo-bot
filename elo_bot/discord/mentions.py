import asyncio  # Preserve concurrent Discord lookup exception behavior.
from collections.abc import Iterable
from typing import Literal

from interactions import Guild, Member, User


async def fetch_members(guild: Guild, user_ids: Iterable[str]) -> dict[str, Member | None]:
    users = list(user_ids)
    members = await asyncio.gather(*(guild.fetch_member(user) for user in users))
    return dict(zip(users, members))


async def member_names(guild: Guild, user_ids: Iterable[str]) -> dict[str, str]:
    """Resolve non-pinging player links concurrently in the publication's guild."""
    return {user: mention_user(member, user) for user, member in (await fetch_members(guild, user_ids)).items()}


def mention_user(user: User | Member | None, user_id: int | str = 0,
                 mention_type: Literal["Player", "Builder", "Unknown Builder", "Helper"] = "Player"):
    if user:
        return f"**[{user.display_name}](<https://discordapp.com/users/{user.id}?tab=wishlist>)**"

    if mention_type == "Player":
        name_filler = "❌Invalid Player"
    elif mention_type == "Builder":
        name_filler = "❓Unknown Builder"
    else:
        name_filler = "❌Invalid Helper"

    return f"**[{name_filler}](<https://discordapp.com/users/{user_id}?tab=wishlist>)**"
