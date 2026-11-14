import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, assert_never

import interactions as ipy

from elobot.features.base_store.live import format_live_slot_line, live_store_components, live_stores
from elobot.config.presentation import COLOR
from elobot.discord.embeds import embed_edit_payload
from elobot.discord.message_edits import edit_channel_message
from elobot.db import Match


type BaseStoreReleaseReason = Literal["undone", "finished", "raid_weekend_ended", "thread_deleted"]
type StoredBaseMessageRetirementReason = BaseStoreReleaseReason | Literal["releasing"]


@dataclass(frozen=True, slots=True)
class StoredBaseMessageRetirement:
    thread_id: str | None
    message_id: int | None
    reason: StoredBaseMessageRetirementReason
    live_reservation_id: str | None = None


async def retire_stored_base_message(
        bot: ipy.Client | None,
        request: StoredBaseMessageRetirement,
) -> None:
    if bot is None or not request.thread_id or request.message_id is None:
        return

    match request.reason:
        case "thread_deleted":
            return
        case "releasing":
            title = "**Releasing Base Storage**"
        case "finished":
            title = "**Base Storage Released**"
        case "raid_weekend_ended":
            title = "**Base Storage Expired**"
        case "undone":
            title = "**Base Storage Undone**"
        case unreachable:
            assert_never(unreachable)

    try:
        channel = await bot.fetch_channel(int(request.thread_id))
        if channel is None:
            return
        message = await bot.cache.fetch_message(channel.id, request.message_id, force=True)
        embeds = deepcopy(message.embeds) or [ipy.Embed(color=COLOR)]
        embeds[0].title = title
        embeds[0].description = None
        components = []
        if request.live_reservation_id and request.reason != "releasing":
            current = await Match.find_one({"thread": str(request.thread_id)})
            retained = live_stores(current, request.live_reservation_id) if current else []
            retained = [(i, store) for i, store in retained if store.message_id == request.message_id]
            if retained:
                from elobot.discord.mentions import mention_user
                user_ids = [current.participants[index].user_id for index, _ in retained]
                members = await asyncio.gather(*(channel.guild.fetch_member(user) for user in user_ids))
                names = {user: mention_user(member, user) for user, member in zip(user_ids, members)}
                embeds[0].title = "**Base Storage Incomplete**"
                status = "\n".join(
                    format_live_slot_line(current.participants[index].user_id, store.slot, names)
                    for index, store in retained
                )
                for field in embeds[0].fields:
                    if field.name == "**Slots**":
                        field.value = status
                components = live_store_components(current, request.live_reservation_id)
        payload = embed_edit_payload(embeds[0])
        payloads = payload if isinstance(payload, list) else [payload]
        payloads.extend(embeds[1:])
        await edit_channel_message(message, embeds=payloads, components=components)
    except ipy.errors.NotFound:
        return
