from collections.abc import Collection

import interactions as ipy

import cogs.base_store.storage as base_store_storage
from elobot.config.game import BASE_STORE_SLOTS


def all_base_store_slots_available(available_slots: Collection[str] | None) -> bool:
    return available_slots is not None and all(slot in available_slots for slot in BASE_STORE_SLOTS)


async def clan_has_all_base_store_slots(clan_tag: str, *, bot: ipy.Client | None = None) -> bool:
    available_by_clan = await base_store_storage.sync_base_store_slots(clan_tag, bot=bot)
    return all_base_store_slots_available(available_by_clan.get(clan_tag))
