from copy import deepcopy
import asyncio
import logging
from typing import assert_never

import interactions as ipy
from pydantic import JsonValue
from pymongo.errors import PyMongoError

from elobot.features.base_store.models import BaseStorePresentationRequest
from elobot.features.base_store.channel_context import ScheduledBaseStoreContext
from elobot.features.base_store.presentation import build_helper_store_components, build_manual_store_components, build_store_embed
from elobot.features.base_store.live import live_stores
from elobot.features.base_store.legacy import resolve_stored_base_message_ids
from elobot.config.emojis import ERROR_EMOJI
from elobot.discord.embeds import embed_edit_payload
from elobot.discord.mentions import mention_user
from elobot.db import Match


def _store_status_payload(embeds: list[ipy.Embed], title: str) -> list[dict[str, JsonValue] | ipy.Embed]:
    embeds[0].title = title
    embeds[0].description = None
    payload = embed_edit_payload(embeds[0])
    payloads = payload if isinstance(payload, list) else [payload]
    payloads.extend(embeds[1:])
    return payloads


async def mark_store_status(
        message: ipy.Message,
        title: str,
        *,
        context: ipy.SlashContext | ipy.ComponentContext | ScheduledBaseStoreContext | None = None,
) -> None:
    payloads = _store_status_payload(deepcopy(message.embeds) or [ipy.Embed()], title)
    if context is not None and ipy.MessageFlags.EPHEMERAL in message.flags:
        await context.edit(message, embeds=payloads, components=[])
    else:
        await message.edit(embeds=payloads, components=[])


async def mark_store_unavailable(
        message: ipy.Message,
        *,
        context: ipy.SlashContext | ipy.ComponentContext | ScheduledBaseStoreContext | None = None,
) -> None:
    await mark_store_status(message, "**Base Storage Unavailable**", context=context)


class BaseStoreMessageConflict(RuntimeError):
    pass


async def mark_store_reserved(
        bot: ipy.Client,
        match: Match,
        player: int,
        *,
        ready: bool = False,
) -> bool:
    store = match.participants[player].base_store
    if store is None:
        return not match.participants[player].opponent_bases_stored
    message_ids = await resolve_stored_base_message_ids(bot, match, player)
    if not message_ids or not match.thread:
        return False
    message_id = store.message_id or message_ids[0]
    if not await match.attach_base_store_message(player, message_id, ready=ready):
        return False
    for stored_message_id in message_ids:
        try:
            channel = await bot.fetch_channel(int(match.thread))
            if channel is None:
                return False
            message = await bot.cache.fetch_message(channel.id, stored_message_id, force=True)
            embeds = deepcopy(message.embeds) or [ipy.Embed()]
            if store.is_live:
                user_ids = {match.participants[index].user_id
                            for index, _ in live_stores(match, store.reservation_id)}
                members = await asyncio.gather(*(channel.guild.fetch_member(user_id) for user_id in user_ids))
                names = {user_id: mention_user(member, user_id) for user_id, member in zip(user_ids, members)}
                for field in embeds[0].fields:
                    if field.name == "**Slots**":
                        field.value = "\n".join(
                            f"{names[match.participants[index].user_id]}: {held.slot}"
                            for index, held in live_stores(match, store.reservation_id)
                        )
            await message.edit(embeds=_store_status_payload(embeds, "**Bases Stored**"))
            if not await match.get_pymongo_collection().find_one(match.base_store_filter(player), {"_id": 1}):
                await mark_store_unavailable(message)
                return False
        except ipy.errors.NotFound:
            continue
    return True


async def report_incomplete_stored_start(
        ctx: ipy.SlashContext | ipy.ComponentContext,
        request: BaseStorePresentationRequest,
) -> None:
    store = request.match.participants[request.player].base_store
    match ctx:
        case ipy.ComponentContext():
            message = ctx.message
            message_id = int(message.id)
        case ipy.SlashContext():
            message = None
            message_id = store.message_id if store is not None else None
        case unreachable:
            assert_never(unreachable)
    try:
        marked_unready = message_id is not None and await request.match.attach_base_store_message(
            request.player, message_id, ready=False,
        )
    except PyMongoError:
        logging.exception("Unable to mark stored bases unready for match %s, side %s", request.match.token, request.player)
        marked_unready = False
    detail = "Helper completed the in-game start, but EloBot did not record the match start. "
    components = []
    if marked_unready:
        detail += "The slot is still reserved. These bases are no longer ready to start. Use Undo before storing again."
        components = build_helper_store_components(request)[1:]
    else:
        detail += "No reservation was released. Its current state could not be confirmed. Please contact a helper."
    try:
        if message is None and message_id is not None and marked_unready:
            message = await ctx.channel.fetch_message(message_id)
        if message is not None:
            embeds = deepcopy(message.embeds) or [ipy.Embed()]
            embeds[0].title = "**Match Start Incomplete**"
            embeds[0].description = detail
            await message.edit(embeds=embeds, components=components)
    except ipy.errors.HTTPException:
        logging.exception(
            "Unable to show incomplete stored-base start for match %s, side %s, message %s",
            request.match.token, request.player, message_id,
        )
    await ctx.send(f"{ERROR_EMOJI} {detail}", ephemeral=True)


async def present_retained_base_store(
        ctx: ipy.SlashContext | ScheduledBaseStoreContext,
        request: BaseStorePresentationRequest,
        *,
        message: ipy.Message | None = None,
        components: list[ipy.Button] | None = None,
) -> bool:
    store = request.match.participants[request.player].base_store
    failure = (
        f"{ERROR_EMOJI} The stored-slot controls could not be confirmed. No reservation was released. "
        "Retry Store to recover Undo, or contact a helper."
    )
    if store is None:
        await ctx.send(failure, ephemeral=True)
        return False
    if components is None:
        components = (build_helper_store_components(request) if request.bases_stored
                      else build_manual_store_components(request, store.slot))
    undo = [button for button in components if button.custom_id.startswith(("helper_stored_undo|", "undo_base_storage|"))]
    player_id = request.match.participants[request.player].user_id
    members = await asyncio.gather(
        ctx.channel.guild.fetch_member(player_id), ctx.channel.guild.fetch_member(request.helper_id),
    )
    names = {
        player_id: mention_user(members[0], player_id),
        request.helper_id: mention_user(members[1], request.helper_id, mention_type="Helper"),
    }
    embed = build_store_embed(request, names)
    embed.title = "**Base Storage Incomplete**"
    pending = deepcopy(embed)
    interaction_message = False
    try:
        if message is None and store.message_id is not None:
            try:
                message = await ctx.bot.cache.fetch_message(ctx.channel.id, store.message_id, force=True)
            except ipy.errors.NotFound:
                message = None
        if message is None:
            try:
                message = await ctx.channel.send(embed=pending, components=[])
            except ipy.errors.HTTPException:
                logging.exception("Unable to post held-slot recovery for match %s", request.match.token)
                message = await ctx.send(embed=pending, components=[], ephemeral=True)
                interaction_message = True
        if await request.match.attach_base_store_message(request.player, int(message.id), ready=False):
            if interaction_message:
                await ctx.edit(message, embed=embed_edit_payload(embed), components=undo)
            else:
                await message.edit(embed=embed_edit_payload(embed), components=undo)
            if await Match.find_one(request.match.base_store_filter(request.player)):
                return True
        await mark_store_unavailable(message, context=ctx if interaction_message else None)
    except (ipy.errors.HTTPException, PyMongoError):
        logging.exception("Unable to recover held-slot controls for match %s", request.match.token)
        if message is not None:
            try:
                await mark_store_unavailable(message, context=ctx if interaction_message else None)
            except ipy.errors.HTTPException:
                logging.exception("Unable to disable failed held-slot recovery %s", message.id)
    await ctx.send(failure, ephemeral=True)
    return False


async def publish_base_store_message(
        ctx: ipy.SlashContext | ScheduledBaseStoreContext,
        request: BaseStorePresentationRequest,
        components: list[ipy.Button],
        *,
        message: ipy.Message | None = None,
) -> bool:
    failure = (
        f"{ERROR_EMOJI} Base storage could not be confirmed. Do not start from this message. "
        "No reservation was released. Use Undo, or retry Store to recover its controls."
    )
    player_id = request.match.participants[request.player].user_id
    members = await asyncio.gather(
        ctx.channel.guild.fetch_member(player_id), ctx.channel.guild.fetch_member(request.helper_id),
    )
    names = {
        player_id: mention_user(members[0], player_id),
        request.helper_id: mention_user(members[1], request.helper_id, mention_type="Helper"),
    }
    embed = build_store_embed(request, names)
    pending = deepcopy(embed)
    pending.title = "**Base Storage**"
    try:
        if message is None:
            message = await ctx.channel.send(embed=pending, components=[])
        if await request.match.attach_base_store_message(
                request.player, int(message.id), ready=request.failure_reason is None,
        ):
            await message.edit(embed=embed_edit_payload(embed), components=components)
            if await Match.find_one(request.match.base_store_filter(request.player)):
                return True
        await mark_store_unavailable(message)
    except (ipy.errors.HTTPException, PyMongoError):
        logging.exception("Unable to confirm base-store message for match %s", request.match.token)
        if not await present_retained_base_store(ctx, request, message=message, components=components):
            return False
    await ctx.send(failure, ephemeral=True)
    return False
