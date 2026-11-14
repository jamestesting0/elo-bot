import interactions as ipy

import elobot.features.base_store.storage as base_store_storage
from elobot.features.base_store.models import numeric_base_store_slot
from elobot.integrations.helper_api import HelperStoredBaseSlot


async def get_available_helper_slot(clan_tag: str, *, bot: ipy.Client | None = None) -> HelperStoredBaseSlot | None:
    available_by_clan = await base_store_storage.sync_base_store_slots(clan_tag, bot=bot)
    available_slots = available_by_clan.get(clan_tag, [])
    if not available_slots:
        return None
    return numeric_base_store_slot(available_slots[0])
