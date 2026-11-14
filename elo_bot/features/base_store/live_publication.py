import logging
import asyncio

import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.features.base_store.models import BaseStoreRuntime
from elo_bot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from elo_bot.features.base_store.publication import mark_store_unavailable
from elo_bot.features.base_store.live import live_pair_ready, live_store_components, live_stores
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.discord.embeds import embed_edit_payload
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.discord.mentions import mention_user
from elo_bot.discord.members import member_guild
from elo_bot.db import Match


async def publish_live_storage(
        runtime: BaseStoreRuntime,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        match: Match,
) -> bool:
    stores = live_stores(match)
    if not stores:
        await ctx.send(f"{ERROR_EMOJI} The live stored-base reservations are no longer available.", ephemeral=True)
        return False
    reservation_id = stores[0][1].reservation_id
    assert reservation_id is not None
    clan = await runtime.coc_client.get_clan(stores[0][1].clan_tag)
    mentions = " ".join(f"<@{participant.user_id}>" for participant in match.participants[:2])
    user_ids = [participant.user_id for participant in match.participants[:2]]
    guild = await member_guild(ctx)
    members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in user_ids))
    names = {user_id: mention_user(member, user_id) for user_id, member in zip(user_ids, members)}
    message = None
    try:
        message_id = stores[0][1].message_id
        if message_id:
            try:
                message = await runtime.bot.cache.fetch_message(ctx.channel.id, message_id, force=True)
            except ipy.errors.NotFound:
                pass
        if message is None:
            message = await ctx.channel.send(content=mentions, embed=ipy.Embed(
                title="**Base Storage**", color=COLOR,
            ), components=[])
        async with BASE_STORE_RESERVATION_LOCK:
            fresh = await Match.find_one({"token": match.token, "thread": str(ctx.channel.id)})
            if fresh is None or [(i, s.reservation_id) for i, s in live_stores(fresh)] != [
                    (i, s.reservation_id) for i, s in stores]:
                await mark_store_unavailable(message)
                return False
            query = {"_id": fresh.id}
            updates = {}
            for index, _ in stores:
                query.update(fresh.base_store_filter(index))
                updates[f"participants.{index}.base_store.message_id"] = int(message.id)
            write = await fresh.get_pymongo_collection().update_one(query, {"$set": updates})
            if write.matched_count != 1:
                await mark_store_unavailable(message)
                return False
            for index, _ in stores:
                fresh.participants[index].base_store.message_id = int(message.id)
                fresh._sync_base_store_state(index)
            ready = live_pair_ready(fresh, reservation_id) and not any(fresh.times) and not any(fresh.hosts)
            statuses = []
            for participant in fresh.participants[:2]:
                store = participant.base_store
                slot = "No slot reserved"
                if store and store.is_live and store.reservation_id == reservation_id:
                    slot = store.slot
                statuses.append(f"{names[participant.user_id]} — {slot}")
            embed = ipy.Embed(
                title="**Bases Stored**" if ready else "**Base Storage Incomplete**",
                description=None,
                fields=[
                    ipy.EmbedField(name="**Players**", value="\n".join(names[user_id] for user_id in user_ids)),
                    ipy.EmbedField(name="**Helper**", value=mention_user(runtime.bot.user)),
                    ipy.EmbedField(name="**Host Clan**", value=f"[{clan.name} ({clan.tag})]({clan.share_link})"),
                    ipy.EmbedField(name="**Slots**", value="\n".join(statuses)),
                ],
                footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE), color=COLOR,
            )
            components = live_store_components(fresh, reservation_id, allow_start=ready)
        await edit_channel_message(
            message, content=mentions, embed=embed_edit_payload(embed),
            components=components,
        )
        if await Match.find_one({**query, **updates}) is None:
            await mark_store_unavailable(message)
            return False
        return True
    except (ipy.errors.HTTPException, PyMongoError):
        logging.exception("Could not publish paired base storage for match %s", match.token)
        if message is not None:
            try:
                await mark_store_unavailable(message)
            except ipy.errors.HTTPException:
                logging.exception("Could not retire paired base-storage message %s", message.id)
        await ctx.send(f"{ERROR_EMOJI} The stored slots remain reserved, but their message could not be published. "
                       "Retry `/self store base-live` to restore its controls.", ephemeral=True)
        return False
