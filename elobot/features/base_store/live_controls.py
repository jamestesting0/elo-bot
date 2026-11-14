import interactions as ipy

from elobot.features.base_store.live_publication import publish_live_storage
from elobot.features.base_store.models import BaseStoreRuntime
from elobot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from elobot.features.hosting.live.request import LiveRequestCoordinator
from elobot.integrations.helper_api import HelperRemoveBasesPayload
from elobot.features.hosting.war.helper_base_store import remove_bases_interactively
from elobot.features.hosting.war.helper_base_store_availability import confirm_remove_bases_availability
from elobot.features.hosting.war.stored_bases import HelperStoredBaseTerminalJob
from elobot.features.base_store.live import live_pair_ready, live_stores
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.features.matches.timing import hosting_started
from elobot.features.results.validation import is_match_finished
from elobot.db import Match


async def resolve_live_storage(ctx: ipy.ComponentContext) -> tuple[Match, str] | None:
    _, token, reservation_id = ctx.custom_id.split("|")
    match = await Match.find_one({"token": token, "thread": str(ctx.channel.id)})
    if match is None or str(ctx.author.id) not in [p.user_id for p in match.participants[:2]]:
        await ctx.send(f"{ERROR_EMOJI} Only a match participant can use these stored bases.", ephemeral=True)
        return None
    stores = live_stores(match, reservation_id)
    if not stores or any(store.message_id != int(ctx.message.id) for _, store in stores):
        await ctx.send(f"{ERROR_EMOJI} This stored-base message is outdated. Use the current storage message.",
                       ephemeral=True)
        return None
    return match, reservation_id


async def _restore_failed_live_start(match: Match, reservation_id: str) -> bool:
    stores = live_stores(match, reservation_id)
    if (
            len(stores) != 2
            or stores[0][1].clan_tag != stores[1][1].clan_tag
            or [store.slot for _, store in stores] != ["Slot 1", "Slot 2"]
            or any(store.message_id is None for _, store in stores)
    ):
        return False
    for index, store in stores:
        if store.ready is True:
            continue
        if not await match.attach_base_store_message(index, int(store.message_id), ready=True):
            return False
    return True


async def request_stored_live_match(coordinator: LiveRequestCoordinator, ctx: ipy.ComponentContext) -> None:
    resolved = await resolve_live_storage(ctx)
    if resolved is None:
        return
    match, reservation_id = resolved
    if hosting_started(match) or any(is_match_finished(match, player=i) for i in (0, 1)):
        await ctx.send(f"{ERROR_EMOJI} This match has already started or finished.", ephemeral=True)
        return
    if not live_pair_ready(match, reservation_id):
        if not await _restore_failed_live_start(match, reservation_id):
            await ctx.send(
                f"{ERROR_EMOJI} Both players' bases must be stored before starting. Use Undo before retrying.",
                ephemeral=True,
            )
            return
        match = await Match.find_one({"token": match.token, "thread": str(ctx.channel.id)})
        if match is None or not live_pair_ready(match, reservation_id):
            await ctx.send(
                f"{ERROR_EMOJI} Both players' bases must be stored before starting. Use Undo before retrying.",
                ephemeral=True,
            )
            return
    requester = next(i for i, p in enumerate(match.participants[:2]) if p.user_id == str(ctx.author.id))
    await coordinator.post(ctx, match, requester, stored_reservation_id=reservation_id)


async def undo_live_storage(runtime: BaseStoreRuntime, ctx: ipy.ComponentContext) -> None:
    resolved = await resolve_live_storage(ctx)
    if resolved is None:
        return
    match, reservation_id = resolved
    leases = []
    try:
        for index in (0, 1):
            lease = await runtime._base_store_try_reserve_side(str(match.token), index)
            if lease is None:
                await ctx.send(f"{ERROR_EMOJI} A stored-base action is already in progress.", ephemeral=True)
                return
            leases.append(lease)
        if not await confirm_remove_bases_availability(runtime, ctx):
            return
        for index, store in live_stores(match, reservation_id):
            fresh = await Match.find_one({"token": match.token, "thread": str(ctx.channel.id)})
            if fresh is None or not any(i == index and s == store for i, s in live_stores(fresh, reservation_id)):
                await ctx.send(f"{ERROR_EMOJI} The stored-base reservation changed. No newer reservation was removed.",
                               ephemeral=True)
                return
            outcome = await remove_bases_interactively(runtime, ctx, HelperRemoveBasesPayload(
                host_clan_tag=store.clan_tag, districts=list(fresh.final_districts), slot=index + 1,
            ))
            if not isinstance(outcome, HelperStoredBaseTerminalJob) or outcome.status != "succeeded":
                await publish_live_storage(runtime, ctx, fresh)
                await ctx.send(f"{ERROR_EMOJI} Helper could not confirm removal of all bases. "
                               "The remaining slots are still reserved.", ephemeral=True)
                return
            async with BASE_STORE_RESERVATION_LOCK:
                if not await runtime._base_store_clear_participant(fresh, index, reset_stored_flag=True, reason="undone"):
                    await ctx.send(f"{ERROR_EMOJI} Slot removal could not be confirmed. Please contact a helper.",
                                   ephemeral=True)
                    return
        await ctx.send(f"{SUCCESS_EMOJI} The stored live bases have been removed and their slots released.", ephemeral=True)
    finally:
        for lease in reversed(leases):
            lease.release()
