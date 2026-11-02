from typing import Protocol

import anyio
import interactions as ipy
from beanie import PydanticObjectId
from pydantic import JsonValue
from pymongo.errors import PyMongoError

from cogs.war_hosting import helper_store, result_store, result_store_ui
from common.constants import ERROR_EMOJI, SUCCESS_EMOJI
from common.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
from common.results.operations import result_input_error
from common.results.validation import is_valid_complete_match_result
from database_models import Match


class LiveResultStoreRuntime(Protocol):
    bot: ipy.Client
    helper_api_base_url: str
    helper_api_key: str

    async def _finalize_match_results_locked(self, token: str) -> bool: ...


def _live_result_identity(match: Match) -> dict[str, JsonValue | PydanticObjectId]:
    return {
        "_id": match.id, "token": str(match.token), "thread": match.thread, "is_war": False,
        "participants.0.user_id": match.participants[0].user_id,
        "participants.1.user_id": match.participants[1].user_id,
        "times.0": match.times[0], "hosts.0": match.hosts[0],
        "host_clans.0": match.host_clans[0], "old_msgs.0": match.old_msgs[0],
        "final_districts": list(match.final_districts),
    }


async def store_live_match_results(
        runtime: LiveResultStoreRuntime,
        ctx: ipy.SlashContext,
        match: Match,
) -> None:
    if match.is_war or len(match.participants) < 2:
        await ctx.send(f"{ERROR_EMOJI} This match is not available for live result storage.", ephemeral=False)
        return
    token = str(match.token)
    generation = PLAYER_DATA_BOUNDARY.generation
    identity = _live_result_identity(match)
    changed = f"{ERROR_EMOJI} The match changed. These results were not saved. Please check the current match."
    lock = result_store.RESULT_AUTOSTORE_LOCKS.setdefault((token, -1), anyio.Lock())
    async with lock:
        source = await Match.find_one(Match.token == token)
        if source is None or source.is_war or len(source.participants) < 2 or _live_result_identity(source) != identity:
            await ctx.send(changed, ephemeral=False)
            return
        if reason := result_input_error(source):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=False)
            return
        outcome = await helper_store.store_helper_score(
            runtime, ctx, helper_store.HelperScoreStoreRequest(match=source, player=None, ephemeral=False),
        )
        if not isinstance(outcome, helper_store.HelperLiveScoreStoreOutcome) or not any(outcome.trusted):
            return

        finalization_lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(token, anyio.Lock())
        try:
            async with finalization_lock, PLAYER_DATA_BOUNDARY.write(generation):
                fresh = await Match.find_one(Match.token == token)
                if (fresh is None or fresh.is_war or len(fresh.participants) < 2
                        or _live_result_identity(fresh) != identity):
                    await ctx.send(changed, ephemeral=False)
                    return
                if reason := result_input_error(fresh):
                    await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=False)
                    return
                query = {**identity, "result_operation": None,
                         "final_result_id": None, "final_result_operation": None, "final_superseded": {"$ne": True},
                         "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}]}
                updates = {}
                results = []
                for player, trusted in enumerate(outcome.trusted):
                    stored = list(fresh.participants[player].result)
                    merged = result_store.merge_trusted_results(stored, fresh.final_districts, trusted)
                    if merged is None:
                        await ctx.send(f"{ERROR_EMOJI} Storage failed because the result found by the bot does not "
                                       "match the existing stored result.", ephemeral=False)
                        return
                    scores, _ = merged
                    if not all(scores) or not is_valid_complete_match_result("\n".join(scores), len(fresh.final_districts)):
                        await ctx.send(f"{ERROR_EMOJI} Storage failed because fewer than three results were found "
                                       "for each player. Neither result was changed.", ephemeral=False)
                        return
                    results.append(scores)
                    query[f"participants.{player}.result"] = stored
                    updates[f"participants.{player}.result"] = scores
                    updates[f"submitters.{player}"] = str(runtime.bot.user.id)
                try:
                    write = await fresh.get_pymongo_collection().update_one(query, {"$set": updates})
                except PyMongoError:
                    await ctx.send(f"{ERROR_EMOJI} The results could not be confirmed as saved. "
                                   "Please contact a helper before retrying.", ephemeral=False)
                    return
                if write.matched_count != 1:
                    await ctx.send(changed, ephemeral=False)
                    return
                for player, scores in enumerate(results):
                    fresh.participants[player].result = scores
                fresh.submitters = [str(runtime.bot.user.id), str(runtime.bot.user.id)]
                fresh._save_state()

                pending = (f"{ERROR_EMOJI} Both results were saved, but match completion could not be confirmed. "
                           "Please contact a helper to finish recording the match.")
                try:
                    finalized = await runtime._finalize_match_results_locked(token)
                except (PyMongoError, ipy.errors.HTTPException):
                    await ctx.send(pending, ephemeral=False)
                    raise
                if not finalized:
                    await ctx.send(pending, ephemeral=False)
                    return
                summaries = [
                    f"<@{participant.user_id}>\n"
                    + result_store_ui.format_result_summary(fresh.final_districts, participant.result)
                    for participant in fresh.participants[:2]
                ]
                await ctx.send(f"{SUCCESS_EMOJI} Both live match results were stored.\n\n" + "\n".join(summaries),
                               ephemeral=False)
                for player, participant in enumerate(fresh.participants[:2]):
                    user = await runtime.bot.fetch_user(int(participant.user_id))
                    await result_store_ui.notify_stored_result(
                        result_store_ui.StoredResultDmRequest(match=fresh, player=player, user=user),
                    )
        except PlayerDataReset as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=False)
