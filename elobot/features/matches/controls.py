from collections.abc import Mapping
from copy import deepcopy

import interactions as ipy
from pydantic import JsonValue

from elobot.config.emojis import ERROR_EMOJI
from elobot.features.matches.messages import MatchMessageState
from elobot.db import Match


async def reject_outdated_match_control(ctx: ipy.ComponentContext | ipy.ModalContext) -> None:
    if ctx.message is not None:
        embeds = deepcopy(ctx.message.embeds) or [ipy.Embed()]
        embeds[0].title = "**Outdated Match Message**"
        embeds[0].description = "This message belongs to an earlier match setup. Use the latest match message."
        await ctx.message.edit(embeds=embeds, components=[])
    await ctx.send(f"{ERROR_EMOJI} The match setup changed. Use the latest match message.", ephemeral=True)


async def check_match_control(
        ctx: ipy.ComponentContext | ipy.ModalContext, match: Match, player: int,
) -> bool:
    if (
            player < 0 or player >= len(match.old_msgs) or ctx.message is None
            or match.old_msgs[player] != int(ctx.message.id)
    ):
        await reject_outdated_match_control(ctx)
        return False
    return True


async def update_match_control(
        match: Match, expected: MatchMessageState, updates: Mapping[str, JsonValue],
) -> bool:
    result = await match.get_pymongo_collection().update_one(expected.binding_filter(), {"$set": dict(updates)})
    return result.matched_count == 1
