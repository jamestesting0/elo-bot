from typing import assert_never

import interactions as ipy

import elo_bot.features.hosting.war.helper_base_store as helper_base_store
import elo_bot.features.hosting.war.helper_base_store_availability as helper_base_store_availability
import elo_bot.features.hosting.war.stored_bases as stored_bases
from elo_bot.features.base_store.preparation import prepare_base_store
from elo_bot.features.base_store.channel_context import ScheduledBaseStoreContext
from elo_bot.features.base_store.raid_policy import eligible_for_raid_storage
from elo_bot.features.base_store.models import (
    BaseStorePreparationFailure,
    BaseStorePreparationRequest,
    BaseStorePresentationRequest,
    BaseStoreRuntime,
    PreparedBaseStore,
    SelfBaseStoreRequest,
    numeric_base_store_slot,
)
from elo_bot.features.base_store.presentation import build_helper_store_components
from elo_bot.features.base_store.publication import mark_store_unavailable, present_retained_base_store, publish_base_store_message
from elo_bot.integrations.helper_api import (
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateRejected,
    HelperJobCreateUnavailable,
    HelperStoreBasesPayload,
    HelperTrackingFailure,
)
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.discord.mentions import mention_user
from elo_bot.db import Match


async def store_self_bases(
        runtime: BaseStoreRuntime,
        ctx: ipy.SlashContext | ScheduledBaseStoreContext,
        request: SelfBaseStoreRequest,
) -> None:
    side_reservation = await runtime._base_store_try_reserve_side(str(request.match.token), request.player)
    if side_reservation is None:
        if not request.raid_autostore:
            await ctx.send(f"{ERROR_EMOJI} A stored-base action is already in progress.", ephemeral=True)
        return
    try:
        current_match = await Match.find_one({"token": request.match.token})
        if current_match is None:
            await ctx.send(f"{ERROR_EMOJI} The match is no longer available.", ephemeral=True)
            return
        if request.raid_autostore and not eligible_for_raid_storage(current_match, request.player):
            return
        helper_id = request.helper_id or str(runtime.bot.user.id)
        if current_match and request.player < len(current_match.participants):
            current_store = current_match.participants[request.player].base_store
            if current_store is not None and current_store.is_live:
                await ctx.send(f"{ERROR_EMOJI} These bases belong to live storage. Use `/self store base-live` "
                               "to recover its controls.", ephemeral=True)
                return
            if current_store is not None:
                clan = await runtime.coc_client.get_clan(current_store.clan_tag)
                presentation = BaseStorePresentationRequest(
                    match=current_match,
                    player=request.player,
                    helper_id=helper_id,
                    clan=clan,
                    failure_reason="A stored-slot reservation already exists.",
                )
                if await present_retained_base_store(ctx, presentation):
                    await ctx.send(
                        f"{ERROR_EMOJI} This slot was already reserved. Its Undo control has been restored.",
                        ephemeral=True,
                    )
                return
        if current_match.times[request.player] or current_match.hosts[request.player]:
            await ctx.send(f"{ERROR_EMOJI} The match has already started. No bases were stored.", ephemeral=True)
            return
        if not request.raid_autostore and not await helper_base_store_availability.confirm_store_bases_availability(
                runtime, ctx,
        ):
            return
        preparation = await prepare_base_store(
            runtime,
            BaseStorePreparationRequest(
                thread_id=str(request.match.thread),
                player=request.player,
                clan=None,
                slot=None,
                raid_autostore=request.raid_autostore,
            ),
        )
        match preparation:
            case BaseStorePreparationFailure(message=message):
                await ctx.send(message, ephemeral=True)
                return
            case PreparedBaseStore(match=prepared_match, clan_tag=clan_tag, slot=slot):
                pass
            case unreachable:
                assert_never(unreachable)
        numeric_slot = numeric_base_store_slot(slot)
        opponent = 1 - request.player
        base_links = [base.link for base in prepared_match.participants[opponent].bases if base]
        failure_reason = None
        pending_message = None
        if numeric_slot is None or not base_links:
            failure_reason = (
                "The selected bases could not be prepared. The reservation was retained because it was already claimed."
            )
        else:
            payload = HelperStoreBasesPayload(host_clan_tag=clan_tag, base_links=base_links, slot=numeric_slot)
            if request.raid_autostore:
                player_id = prepared_match.participants[request.player].user_id
                member = await ctx.channel.guild.fetch_member(player_id)
                pending_message = await ctx.channel.send(
                    embed=ipy.Embed(title="**Base Storage**", description=(
                        f"Automatically storing bases for {mention_user(member, player_id)} "
                        "before raid weekend."
                    )),
                    components=[],
                )
                if not await prepared_match.attach_base_store_message(
                        request.player, int(pending_message.id), ready=False,
                ):
                    await mark_store_unavailable(pending_message)
                    await ctx.send(f"{ERROR_EMOJI} The slot reservation changed. No Helper storage was requested.")
                    return
                outcome = await stored_bases.store_bases(runtime, str(runtime.bot.user.id), payload)
            else:
                outcome = await helper_base_store.store_bases_interactively(runtime, ctx, payload)
            match outcome:
                case stored_bases.HelperStoredBaseTerminalJob(status="succeeded"):
                    pass
                case HelperJobCreateRejected():
                    failure_reason = "Helper rejected the store request after this slot was reserved."
                case stored_bases.HelperStoredBaseTerminalJob(status="failed" | "cancelled"):
                    failure_reason = "Helper stopped before confirming that all bases were stored."
                case HelperTrackingFailure():
                    failure_reason = "EloBot could not confirm whether Helper finished storing the bases."
                case HelperJobCreateAcceptedInvalid() | HelperJobCreateUnavailable():
                    failure_reason = "Helper returned an uncertain state after this slot was reserved."
                case unreachable:
                    assert_never(unreachable)
        clan = await runtime.coc_client.get_clan(clan_tag)
        presentation = BaseStorePresentationRequest(
            match=prepared_match,
            player=request.player,
            helper_id=helper_id,
            clan=clan,
            failure_reason=failure_reason,
        )
        if failure_reason:
            published = await present_retained_base_store(ctx, presentation, message=pending_message)
        else:
            published = await publish_base_store_message(
                ctx, presentation, build_helper_store_components(presentation), message=pending_message,
            )
        if published:
            response = (
                f"{ERROR_EMOJI} {failure_reason}" if failure_reason
                else f"{SUCCESS_EMOJI} The bases were stored successfully."
            )
            await ctx.send(response, ephemeral=True)
    finally:
        side_reservation.release()
