from collections.abc import Mapping
from copy import deepcopy

import interactions as ipy
from pydantic import JsonValue
from beanie import PydanticObjectId

from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.features.matches.messages import MatchMessageState
from elo_bot.db import Match
from elo_bot.core.finals import FinalError, is_final_match
from elo_bot.features.finals.matches import require_ongoing_match


async def reject_outdated_match_control(ctx: ipy.ComponentContext | ipy.ModalContext) -> None:
    # Answer first: retiring the public message waits on the channel edit queue.
    await ctx.send(f"{ERROR_EMOJI} The match setup changed. Use the latest match message.", ephemeral=True)
    if ctx.message is not None:
        embeds = deepcopy(ctx.message.embeds) or [ipy.Embed()]
        embeds[0].title = "**Outdated Match Message**"
        embeds[0].description = "This message belongs to an earlier match setup. Use the latest match message."
        await edit_channel_message(ctx.message, embeds=embeds, components=[])


async def check_match_control(
        ctx: ipy.ComponentContext | ipy.ModalContext, match: Match, player: int,
) -> bool:
    if not await check_current_match(ctx, match):
        return False
    if (
            player < 0 or player >= len(match.old_msgs) or ctx.message is None
            or match.old_msgs[player] != int(ctx.message.id)
    ):
        await reject_outdated_match_control(ctx)
        return False
    return True


async def check_current_match(ctx: ipy.InteractionContext, match: Match) -> bool:
    if is_final_match(match):
        try:
            await require_ongoing_match(match)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return False
    return True


async def update_match(
        match: Match, query: Mapping[str, JsonValue | PydanticObjectId], update: Mapping[str, JsonValue],
) -> bool:
    """Final cancellation and replacement also change the physical match in their transaction."""
    condition = dict(query)
    if is_final_match(match):
        try:
            await require_ongoing_match(match)
        except FinalError:
            return False
        condition.update({"final_id": match.final_id, "final_node_id": match.final_node_id,
            "final_contestant_revision": match.final_contestant_revision, "final_attempt": match.final_attempt,
            "final_superseded": False, "final_result_id": None, "final_result_operation": None})
    result = await match.get_pymongo_collection().update_one(condition, dict(update))
    return result.matched_count == 1


async def update_match_control(
        match: Match, expected: MatchMessageState, updates: Mapping[str, JsonValue],
) -> bool:
    return await update_match(match, expected.binding_filter(), {"$set": dict(updates)})
