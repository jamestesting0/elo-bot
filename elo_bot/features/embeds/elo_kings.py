from collections.abc import Sequence
from dataclasses import dataclass
import re

import anyio
import interactions as ipy
from aiohttp import ClientError
from beanie.operators import In
from pydantic import BaseModel

from elo_bot.db import Player
from elo_bot.features.finals.rendering import ELO_KINGS_ROWS


ELO_KING = re.compile(r"Elo King:\s*\*\*\[([^\]]+)\]\(<https://discord(?:app)?\.com/users/(\d+)>\)")


class PeakRating(BaseModel):
    user_id: str
    highest_elo: int


@dataclass(frozen=True, slots=True)
class EloKing:
    user_id: str
    name: str
    highest_elo: int


async def elo_king_rows(information: str) -> list[EloKing]:
    names = {user_id: name for name, user_id in ELO_KING.findall(information)}
    if not names:
        return []
    players = await Player.find(In(Player.user_id, list(names))).project(PeakRating).sort(
        "-highest_elo", "user_id",
    ).limit(ELO_KINGS_ROWS).to_list()
    return [EloKing(player.user_id, names[player.user_id], player.highest_elo) for player in players]


async def elo_king_avatars(bot: ipy.Client, rows: Sequence[EloKing]) -> dict[str, bytes]:
    avatars: dict[str, bytes] = {}

    async def fetch_avatar(user_id: str) -> None:
        try:
            with anyio.fail_after(5):
                user = await bot.fetch_user(user_id)
                if user is not None:
                    avatars[user_id] = await user.display_avatar.fetch(extension=".png", size=64)
        except (ClientError, ipy.errors.HTTPException, OSError, TimeoutError):
            return

    async with anyio.create_task_group() as tasks:
        for row in rows:
            tasks.start_soon(fetch_avatar, row.user_id)
    return avatars
