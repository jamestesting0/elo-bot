from dataclasses import dataclass
import logging
from typing import Protocol

import anyio
import interactions as ipy
from beanie import PydanticObjectId
from pydantic import JsonValue
from pymongo.errors import PyMongoError

from cogs.war_hosting import helper_store, result_store_ui
from cogs.war_hosting.helper_api import HelperScoreAttackLimit
from common.constants import ERROR_EMOJI, SUCCESS_EMOJI
from common.results.validation import is_match_finished, is_valid_complete_match_result
from common.results.operations import result_input_error
from common.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
from database_models import Match


RESULT_FINALIZATION_LOCKS: dict[str, anyio.Lock] = {}
RESULT_AUTOSTORE_LOCKS: dict[tuple[str, int], anyio.Lock] = {}
LOGGER = logging.getLogger(__name__)

type ResultStoreContext = helper_store.HelperScoreStoreContext


@dataclass(frozen=True, slots=True)
class FinalizeMatchResultsRequest:
    token: str
    response: dict[str, str] | None = None
    ctx: ipy.BaseContext | None = None
    data_generation: int | None = None


@dataclass(frozen=True, slots=True)
class AutomaticResultStoreRequest:
    match: Match
    player: int
    max_allowed_attacks: HelperScoreAttackLimit = 2
    interactive: bool = True
    expected_started_at: int | None = None
    scheduled_delay_minutes: int | None = None
    include_response_result: bool = False
    allow_cancellation: bool = True
    submitter_id: str | None = None


class ResultStoreRuntime(Protocol):
    bot: ipy.Client
    helper_api_base_url: str
    helper_api_key: str

    async def _finalize_match_results_locked(
            self,
            token: str,
            response: dict[str, str] | None = None,
            ctx: ipy.BaseContext | None = None,
    ) -> bool: ...

    async def _finish_stored_result_side(self, match: Match, player: int) -> None: ...


def _automatic_result_store_should_skip(match: Match, request: AutomaticResultStoreRequest) -> bool:
    player = request.player
    if request.expected_started_at is not None and match.times[player] != request.expected_started_at:
        return True
    return not request.interactive and (
        not match.is_war
        or not match.host_clans[player]
        or not match.thread
        or len(match.final_districts) not in {3, 7}
        or not all(isinstance(district, str) and district for district in match.final_districts)
        or is_match_finished(match, player=player)
    )


async def finalize_match_results(
        runtime: ResultStoreRuntime,
        request: FinalizeMatchResultsRequest,
) -> bool:
    data_generation = request.data_generation
    if data_generation is None:
        data_generation = PLAYER_DATA_BOUNDARY.generation
    lock = RESULT_FINALIZATION_LOCKS.setdefault(request.token, anyio.Lock())
    try:
        async with lock, PLAYER_DATA_BOUNDARY.write(data_generation):
            return await runtime._finalize_match_results_locked(
                request.token,
                response=request.response,
                ctx=request.ctx,
            )
    except PlayerDataReset as error:
        if request.ctx is not None:
            await request.ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
        return False


def complete_stored_results(match: Match) -> bool:
    return len(match.participants) >= 2 and all(
        len(participant.result) == len(match.final_districts)
        and all(isinstance(result, str) and result for result in participant.result)
        and is_valid_complete_match_result("\n".join(participant.result), len(match.final_districts))
        for participant in match.participants[:2]
    )


def merge_trusted_results(
        stored_results: list[str | None],
        final_districts: list[str | None],
        trusted: tuple[tuple[str | None, str], ...],
) -> tuple[list[str | None], bool] | None:
    merged_results = stored_results.copy()
    applied_trusted = False
    for district, score in trusted:
        if district not in final_districts:
            continue
        district_index = final_districts.index(district)
        stored_result = stored_results[district_index]
        if stored_result and stored_result != score:
            return None
        merged_results[district_index] = score
        applied_trusted = True
    return merged_results, applied_trusted


def _result_store_identity(match: Match, player: int) -> dict[str, JsonValue | PydanticObjectId]:
    return {
        "_id": match.id, "token": str(match.token), "thread": match.thread, "is_war": match.is_war,
        f"participants.{player}.user_id": match.participants[player].user_id,
        f"times.{player}": match.times[player], f"hosts.{player}": match.hosts[player],
        f"host_clans.{player}": match.host_clans[player], f"old_msgs.{player}": match.old_msgs[player],
        "final_districts": list(match.final_districts),
    }


async def store_match_result_automatically(
        runtime: ResultStoreRuntime,
        ctx: ResultStoreContext,
        request: AutomaticResultStoreRequest,
) -> None:
    data_generation = PLAYER_DATA_BOUNDARY.generation
    token = str(request.match.token)
    player = request.player
    if player not in {0, 1} or len(request.match.participants) <= player:
        await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match.", ephemeral=True)
        return
    expected_identity = _result_store_identity(request.match, player)
    changed_message = f"{ERROR_EMOJI} The match changed. These results were not saved. Please check the current match."
    lock = RESULT_AUTOSTORE_LOCKS.setdefault((token, player), anyio.Lock())
    async with lock:
        source_match = await Match.find_one(Match.token == token)
        if source_match is None or player not in {0, 1} or len(source_match.participants) <= player:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match.", ephemeral=True)
            return
        if reason := result_input_error(source_match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return
        if not source_match.is_war:
            if not request.interactive:
                return
            await ctx.send(
                f"{ERROR_EMOJI} This match is not available for automatic result storage.",
                ephemeral=True,
            )
            return
        if _automatic_result_store_should_skip(source_match, request):
            return
        if _result_store_identity(source_match, player) != expected_identity:
            await ctx.send(changed_message, ephemeral=True)
            return

        outcome = await helper_store.store_helper_score(
            runtime,
            ctx,
            helper_store.HelperScoreStoreRequest(
                match=source_match,
                player=player,
                interactive=request.interactive,
                allow_cancellation=request.allow_cancellation,
                max_allowed_attacks=request.max_allowed_attacks,
            ),
        )
        if not outcome.trusted:
            return

        finalization_lock = RESULT_FINALIZATION_LOCKS.setdefault(token, anyio.Lock())
        try:
            async with finalization_lock, PLAYER_DATA_BOUNDARY.write(data_generation):
                fresh_match = await Match.find_one(Match.token == token)
                if fresh_match is None or len(fresh_match.participants) <= player:
                    await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match.", ephemeral=True)
                    return
                if reason := result_input_error(fresh_match):
                    await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
                    return
                if _result_store_identity(fresh_match, player) != expected_identity:
                    await ctx.send(changed_message, ephemeral=True)
                    return
                if _automatic_result_store_should_skip(fresh_match, request):
                    return

                stored_results = list(fresh_match.participants[player].result)
                merge_outcome = merge_trusted_results(stored_results, fresh_match.final_districts, outcome.trusted)
                if merge_outcome is None:
                    await ctx.send(
                        f"{ERROR_EMOJI} Storage failed because the result found by the bot does not match "
                        "the existing stored result.",
                        ephemeral=True,
                    )
                    return
                merged_results, applied_trusted = merge_outcome
                if not applied_trusted or merged_results == stored_results:
                    return

                result_is_complete = (
                    len(merged_results) == len(fresh_match.final_districts)
                    and all(isinstance(result, str) and result for result in merged_results)
                    and is_valid_complete_match_result("\n".join(merged_results), len(fresh_match.final_districts))
                )
                if not result_is_complete:
                    expected_count = "seven" if len(fresh_match.final_districts) == 7 else "three"
                    await ctx.send(
                        f"{ERROR_EMOJI} Storage failed because fewer than {expected_count} results were found.",
                        ephemeral=True,
                    )
                    return

                submitters = list(fresh_match.submitters or [None, None])
                submitters[player] = (
                    request.submitter_id
                    if request.submitter_id is not None
                    else str(ctx.author.id)
                )
                updates = {
                    f"participants.{player}.result": merged_results,
                    f"submitters.{player}": submitters[player],
                }
                try:
                    write = await fresh_match.get_pymongo_collection().update_one(
                        {**expected_identity, f"participants.{player}.result": stored_results, "result_operation": None,
                         "final_result_id": None, "final_result_operation": None, "final_superseded": {"$ne": True},
                         "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}]},
                        {"$set": updates},
                    )
                except PyMongoError:
                    LOGGER.exception("Could not confirm result storage for match %s side %s", token, player)
                    await ctx.send(
                        f"{ERROR_EMOJI} The result could not be confirmed as saved. Please contact a helper before retrying.",
                        ephemeral=True,
                    )
                    return
                if write.matched_count != 1:
                    current = await Match.find_one(Match.token == token)
                    reason = result_input_error(current) if current else None
                    await ctx.send(f"{ERROR_EMOJI} {reason}" if reason else changed_message, ephemeral=True)
                    return
                fresh_match.participants[player].result = merged_results
                fresh_match.submitters = submitters
                fresh_match._save_state()

                try:
                    await runtime._finish_stored_result_side(fresh_match, player)
                finally:
                    if complete_stored_results(fresh_match):
                        await runtime._finalize_match_results_locked(token)

                participant = fresh_match.participants[player]
                player_user = await runtime.bot.fetch_user(int(participant.user_id))
                if request.include_response_result:
                    result_summary = result_store_ui.format_result_summary(
                        fresh_match.final_districts,
                        participant.result,
                    )
                    await ctx.send(
                        f"{SUCCESS_EMOJI} Helper found a complete result.\n\n"
                        f"{result_summary}",
                        ephemeral=True,
                    )
                elif request.scheduled_delay_minutes is None:
                    success_message = f"{SUCCESS_EMOJI} You have stored the match result for {player_user.mention}."
                    await ctx.send(success_message, ephemeral=True)
                else:
                    success_message = (
                        f"{SUCCESS_EMOJI} Automatic result storage ran {request.scheduled_delay_minutes} minutes after "
                        f"Helper finished hosting this war match and stored the match result for {player_user.mention}."
                    )
                    await ctx.send(success_message, ephemeral=True)
                await result_store_ui.notify_stored_result(
                    result_store_ui.StoredResultDmRequest(
                        match=fresh_match,
                        player=player,
                        user=player_user,
                    )
                )
        except PlayerDataReset as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
