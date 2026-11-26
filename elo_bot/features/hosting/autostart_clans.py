import interactions as ipy

from elo_bot.config.game import HOST_CLAN_CHOICES
from elo_bot.db import Clan
from elo_bot.db.sequential_models import HelperSequentialRun
from elo_bot.features.base_store.repository import sync_base_store_slots
from elo_bot.features.hosting.live.eligibility import all_base_store_slots_available
from elo_bot.features.matches.resources import is_resource_clan_locked


async def available_autostart_clans(bot: ipy.Client, *, live: bool, preferred: str | None) -> list[str]:
    configured = [str(choice.value) for choice in HOST_CLAN_CHOICES]
    clans = await Clan.find({"tag": {"$in": configured}, "availability_str": "Available"}).to_list()
    available = {clan.tag for clan in clans if not is_resource_clan_locked(clan.tag)}
    if not available:
        return []
    runs = await HelperSequentialRun.find({"host_clan_tag": {"$in": sorted(available)}, "open": True}).to_list()
    available.difference_update(run.host_clan_tag for run in runs)
    if not available:
        return []
    slots = await sync_base_store_slots(bot=bot)
    candidates = [tag for tag in configured if tag in available and slots.get(tag)
                  and (not live or all_base_store_slots_available(slots[tag]))]
    return sorted(candidates, key=lambda tag: tag != preferred)
