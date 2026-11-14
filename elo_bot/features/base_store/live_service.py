import logging

import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.features.base_store.live_preparation import live_storage_error, prepare_live_storage
from elo_bot.features.base_store.live_publication import publish_live_storage
from elo_bot.features.base_store.models import BaseStorePreparationFailure, BaseStoreRuntime
from elo_bot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from elo_bot.features.hosting.live.live_reservations import LiveStartReservations
import elo_bot.features.hosting.live.live_start as live_start
from elo_bot.integrations.helper_api import HelperStoreBasesPayload
from elo_bot.features.hosting.war.helper_base_store import store_bases_interactively
from elo_bot.features.hosting.war.helper_base_store_availability import confirm_store_bases_availability
from elo_bot.features.hosting.war.stored_bases import HelperStoredBaseTerminalJob
from elo_bot.features.base_store.live import live_base_snapshot_filter, live_pair_ready, live_stores
from elo_bot.config.game import BASE_STORE_SLOTS, HOST_CLAN_CHOICES
from elo_bot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elo_bot.db import Match


async def store_self_live_bases(runtime: BaseStoreRuntime, ctx: ipy.SlashContext, source: Match) -> None:
    if live_stores(source):
        leases = []
        try:
            for index in (0, 1):
                lease = await runtime._base_store_try_reserve_side(str(source.token), index)
                if lease is None:
                    await ctx.send(f"{ERROR_EMOJI} A stored-base action is already in progress.", ephemeral=True)
                    return
                leases.append(lease)
            if await publish_live_storage(runtime, ctx, source):
                reservation_id = live_stores(source)[0][1].reservation_id
                restored = reservation_id and live_pair_ready(source, reservation_id)
                await ctx.send(
                    f"{SUCCESS_EMOJI} Live stored-base controls were restored." if restored
                    else (
                        f"{ERROR_EMOJI} Base storage is incomplete. The slots remain reserved; "
                        "use Undo before retrying."
                    ),
                    ephemeral=True,
                )
        finally:
            for lease in reversed(leases):
                lease.release()
        return
    if reason := live_storage_error(source):
        await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
        return
    if not await confirm_store_bases_availability(runtime, ctx):
        return
    await ctx.send(f"{LOADING_EMOJI} Storing both players' bases…", ephemeral=True)
    slots = await runtime._base_store_sync_slots()
    clan_tag = None
    for choice in HOST_CLAN_CHOICES:
        tag = str(choice.value)
        if all(slot in slots.get(tag, []) for slot in BASE_STORE_SLOTS) and await runtime._base_store_is_clan_available(tag):
            clan_tag = tag
            break
    if clan_tag is None:
        await ctx.send(f"{ERROR_EMOJI} There is no available helper clan with both base slots free.", ephemeral=True)
        return
    reservations = await live_start.reserve_live_start(str(source.token), clan_tag)
    if not isinstance(reservations, LiveStartReservations):
        await ctx.send(f"{ERROR_EMOJI} The match or its helper clan is already in use. Please try again.", ephemeral=True)
        return
    try:
        prepared = await prepare_live_storage(runtime, source, clan_tag)
        if isinstance(prepared, BaseStorePreparationFailure):
            await ctx.send(prepared.message, ephemeral=True)
            return
        await runtime._base_store_sync_slots(clan_tag)
        complete = True
        for index in (0, 1):
            query = {"_id": prepared.id, "thread": prepared.thread,
                     "times": list(prepared.times), "hosts": list(prepared.hosts),
                     "final_districts": list(prepared.final_districts), **live_base_snapshot_filter(prepared)}
            for side in (0, 1):
                query.update(prepared.base_store_filter(side))
            async with BASE_STORE_RESERVATION_LOCK:
                if await Match.find_one(query) is None:
                    complete = False
                    break
            outcome = await store_bases_interactively(runtime, ctx, HelperStoreBasesPayload(
                host_clan_tag=clan_tag, slot=index + 1,
                base_links=[base.link for base in prepared.participants[1 - index].bases if base],
            ))
            if not isinstance(outcome, HelperStoredBaseTerminalJob) or outcome.status != "succeeded":
                complete = False
                break
            async with BASE_STORE_RESERVATION_LOCK:
                write = await prepared.get_pymongo_collection().update_one(
                    query, {"$set": {f"participants.{index}.base_store.ready": True}},
                )
                if write.matched_count != 1:
                    complete = False
                    break
                prepared.participants[index].base_store.ready = True
                prepared._sync_base_store_state(index)
        if await publish_live_storage(runtime, ctx, prepared):
            await ctx.send(
                f"{SUCCESS_EMOJI} Both players' bases were stored successfully." if complete
                else f"{ERROR_EMOJI} Base storage is incomplete. The slots remain reserved; use Undo before retrying.",
                ephemeral=True,
            )
    except (ipy.errors.HTTPException, PyMongoError):
        logging.exception("Paired base storage could not be completed for match %s", source.token)
        await ctx.send(f"{ERROR_EMOJI} Base storage could not be confirmed. No reservations were released. "
                       "Retry `/self store base-live` to recover its controls.", ephemeral=True)
    finally:
        reservations.release()
