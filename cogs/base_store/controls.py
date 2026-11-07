import logging
from typing import assert_never

import interactions as ipy

import cogs.war_hosting.helper_base_store as helper_base_store
import cogs.war_hosting.helper_base_store_availability as helper_base_store_availability
import cogs.war_hosting.stored_bases as stored_bases
import cogs.war_hosting.result_store_task as result_store_task
from cogs.base_store.models import (
    BaseStoreRuntime,
    BaseStorePresentationRequest,
    StoredBaseActionRequest,
    StoredBaseControlFailure,
    StoredBaseControlOutcome,
    StoredBaseControlState,
    numeric_base_store_slot,
    stored_base_matches,
    bind_legacy_store_message,
)
from cogs.base_store.publication import (
    BaseStoreMessageConflict, mark_store_status, mark_store_unavailable, report_incomplete_stored_start,
)
from cogs.war_hosting.helper_api import (
    HelperHostWarMatchOnStoredPayload,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateRejected,
    HelperJobCreateUnavailable,
    HelperRemoveBasesPayload,
    HelperTrackingFailure,
)
from common.constants import ERROR_EMOJI, SUCCESS_EMOJI
from common.finals.types import is_final_match
from common.matches.messages import MatchMessageState
from common.results.validation import is_match_finished
from common.utils import get_participant_base_store
from database_models import Match, Player
from common.finals.hosting import accept_final_start
from common.matches.timing import hosting_started
from common.finals.types import FinalError


async def resolve_stored_base_control(
        request: StoredBaseActionRequest, *, allow_live: bool = False,
) -> StoredBaseControlOutcome:
    match = await Match.find_one({"token": request.token})
    if not match or request.player < 0 or request.player >= len(match.participants):
        return StoredBaseControlFailure(
            f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended."
        )
    store = match.participants[request.player].base_store
    if store and store.is_live and not allow_live:
        return StoredBaseControlFailure(f"{ERROR_EMOJI} Use the paired live-storage message for these bases.")
    clan_tag, slot = get_participant_base_store(match.participants[request.player])
    numeric_slot = numeric_base_store_slot(slot) if slot else None
    if not clan_tag or not slot or numeric_slot is None or not stored_base_matches(match, request):
        return StoredBaseControlFailure(
            f"{ERROR_EMOJI} The stored-base reservation is no longer available."
        )
    if len(match.final_districts) not in {3, 7} or not all(match.final_districts):
        return StoredBaseControlFailure(
            f"{ERROR_EMOJI} This match does not have three final districts yet."
        )
    if not await bind_legacy_store_message(match, request.player, request.message_id):
        return StoredBaseControlFailure(f"{ERROR_EMOJI} The stored-base reservation changed. Please try again.")
    return StoredBaseControlState(
        match=match,
        player=request.player,
        clan_tag=clan_tag,
        slot=slot,
        numeric_slot=numeric_slot,
        districts=[district for district in match.final_districts if district],
    )


def _schedule_stored_result_store(
        runtime: BaseStoreRuntime, ctx: ipy.SlashContext | ipy.ComponentContext, state: StoredBaseControlState,
) -> None:
    started_at = state.match.times[state.player]
    if isinstance(ctx, ipy.SlashContext) and started_at is not None:
        result_store_task.schedule_automatic_result_store(runtime.bot, result_store_task.ScheduledResultStoreRequest(
            token=str(state.match.token), player=state.player, requested_by=str(ctx.author.id),
            started_at=started_at, submitter_id=str(runtime.bot.user.id), is_final=is_final_match(state.match),
        ))


async def start_stored_bases(
        runtime: BaseStoreRuntime,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        request: StoredBaseActionRequest,
) -> None:
    if not await helper_base_store_availability.confirm_host_war_match_on_stored_availability(runtime, ctx):
        return
    side_reservation = await runtime._base_store_try_reserve_side(request.token, request.player)
    if not side_reservation:
        await ctx.send(f"{ERROR_EMOJI} This stored-base action is already in progress.", ephemeral=True)
        return
    try:
        control = await resolve_stored_base_control(request, allow_live=isinstance(ctx, ipy.SlashContext))
        match control:
            case StoredBaseControlFailure(message=message):
                if isinstance(ctx, ipy.ComponentContext):
                    await mark_store_unavailable(ctx.message, context=ctx)
                await ctx.send(message, ephemeral=True)
                return
            case StoredBaseControlState() as state:
                pass
            case unreachable:
                assert_never(unreachable)
        if is_match_finished(state.match, player=request.player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return
        if hosting_started(state.match, request.player):
            await ctx.send(f"{ERROR_EMOJI} The match has already started.", ephemeral=True)
            return
        store = state.match.participants[request.player].base_store
        if store is not None and store.ready is False:
            await ctx.send(
                f"{ERROR_EMOJI} Base storage is incomplete. Use Undo before trying again.", ephemeral=True,
            )
            return
        player_doc = await Player.find_one({"user_id": state.match.participants[request.player].user_id})
        player_tag = player_doc.tag if player_doc else None
        if not player_tag:
            await ctx.send(f"{ERROR_EMOJI} Player not found in database.", ephemeral=True)
            return
        if not await runtime._base_store_confirm_pending(ctx, state):
            return
        clan = await runtime.coc_client.get_clan(state.clan_tag)
        clan_reservation = await runtime._base_store_try_reserve_clan(state.clan_tag)
        if not clan_reservation:
            await ctx.send(f"{ERROR_EMOJI} The stored-base clan is currently occupied.", ephemeral=True)
            return
        try:
            if not await runtime._base_store_is_clan_available(state.clan_tag):
                await ctx.send(f"{ERROR_EMOJI} The stored-base clan is currently occupied.", ephemeral=True)
                return
            try:
                await accept_final_start(state.match, str(ctx.author.id), clan_tag=state.clan_tag, player=request.player,
                    expected_source={**MatchMessageState.capture(state.match, request.player).binding_filter(),
                                     **state.match.base_store_filter(request.player)} if is_final_match(state.match) else None)
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                return
            outcome = await helper_base_store.host_war_match_on_stored_interactively(
                runtime,
                ctx,
                HelperHostWarMatchOnStoredPayload(
                    host_clan_tag=state.clan_tag,
                    player_tag=player_tag,
                    districts=state.districts,
                    slot=state.numeric_slot,
                    remove_bases_after=False,
                ),
            )
            match outcome:
                case stored_bases.HelperStoredBaseTerminalJob(status="succeeded"):
                    pass
                case (
                    stored_bases.HelperStoredBaseTerminalJob(status="failed" | "cancelled")
                    | HelperTrackingFailure()
                ):
                    return
                case (
                    HelperJobCreateRejected()
                    | HelperJobCreateAcceptedInvalid()
                    | HelperJobCreateUnavailable()
                ):
                    await ctx.send(
                        f"{ERROR_EMOJI} Helper could not confirm the automatic match start.",
                        ephemeral=True,
                    )
                    return
                case unreachable:
                    assert_never(unreachable)
            presentation = BaseStorePresentationRequest(state.match, state.player, str(runtime.bot.user.id), clan)
            try:
                started_message = await runtime._base_store_start_local(ctx, state, clan)
            except BaseStoreMessageConflict:
                logging.exception(
                    "Stored-base start committed but its message update failed for match %s, side %s",
                    state.match.token, state.player,
                )
                _schedule_stored_result_store(runtime, ctx, state)
                await ctx.send(
                    f"{ERROR_EMOJI} Helper started the match and its start was saved, but the stored-base "
                    "message could not be updated. No reservation was released by this start. Please contact a helper.",
                    ephemeral=True,
                )
                return
            except Exception:  # noqa: BROAD_EXCEPT_OK - preserve storage after failures before the durable start commit
                refreshed_match = await Match.find_one({"token": request.token})
                if refreshed_match and hosting_started(refreshed_match, request.player):
                    raise
                await report_incomplete_stored_start(ctx, presentation)
                return
            if not started_message:
                await report_incomplete_stored_start(ctx, presentation)
            else:
                _schedule_stored_result_store(runtime, ctx, state)
        finally:
            clan_reservation.release()
    finally:
        side_reservation.release()


async def undo_stored_bases(
        runtime: BaseStoreRuntime,
        ctx: ipy.ComponentContext,
        request: StoredBaseActionRequest,
) -> None:
    if not await helper_base_store_availability.confirm_remove_bases_availability(runtime, ctx):
        return
    side_reservation = await runtime._base_store_try_reserve_side(request.token, request.player)
    if not side_reservation:
        await ctx.send(f"{ERROR_EMOJI} This stored-base action is already in progress.", ephemeral=True)
        return
    try:
        control = await resolve_stored_base_control(request)
        match control:
            case StoredBaseControlFailure(message=message):
                await mark_store_unavailable(ctx.message, context=ctx)
                await ctx.send(message, ephemeral=True)
                return
            case StoredBaseControlState() as state:
                pass
            case unreachable:
                assert_never(unreachable)
        outcome = await helper_base_store.remove_bases_interactively(
            runtime,
            ctx,
            HelperRemoveBasesPayload(
                host_clan_tag=state.clan_tag,
                districts=state.districts,
                slot=state.numeric_slot,
            ),
        )
        match outcome:
            case stored_bases.HelperStoredBaseTerminalJob(status="succeeded"):
                cleared = await runtime._base_store_clear_participant(
                    state.match, request.player, reset_stored_flag=True, reason="undone",
                )
                if not cleared:
                    failure = f"{ERROR_EMOJI} The reservation changed. Its removal could not be confirmed."
                    await mark_store_unavailable(ctx.message, context=ctx)
                    await ctx.send(failure, ephemeral=True)
                    return
                await mark_store_status(ctx.message, "**Base Storage Undone**", context=ctx)
                await ctx.send(f"{SUCCESS_EMOJI} This stored-base action has been undone.", ephemeral=True)
            case (
                stored_bases.HelperStoredBaseTerminalJob(status="failed" | "cancelled")
                | HelperTrackingFailure()
            ):
                return
            case (
                HelperJobCreateRejected()
                | HelperJobCreateAcceptedInvalid()
                | HelperJobCreateUnavailable()
            ):
                await ctx.send(
                    f"{ERROR_EMOJI} Helper could not confirm removal of the stored bases.",
                    ephemeral=True,
                )
            case unreachable:
                assert_never(unreachable)
    finally:
        side_reservation.release()
