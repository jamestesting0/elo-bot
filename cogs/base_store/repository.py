import interactions as ipy

from common.base_store.messages import StoredBaseMessageRetirement, retire_stored_base_message
from common.base_store.legacy import resolve_stored_base_message_ids
from common.constants import BASE_STORE_SLOTS
from common.utils import (
    collect_base_store_slot_owners,
    get_participant_base_store,
    is_unmapped_base_store_reservation,
    latest_completed_raid_weekend_end_timestamp,
)
from database_models import Clan, Match, Participant


def collect_occupied_base_store_slots(matches: list[Match]) -> dict[str, set[str]]:
    occupied_slots: dict[str, set[str]] = {}
    for match in matches:
        if not getattr(match, "thread", None):
            continue
        for participant in getattr(match, "participants", []) or []:
            clan_tag, slot = get_participant_base_store(participant)
            if clan_tag and slot in BASE_STORE_SLOTS:
                occupied_slots.setdefault(clan_tag, set()).add(slot)
    return occupied_slots


def get_base_store_timestamp(participant: Participant) -> int | None:
    base_store = getattr(participant, "base_store", None)
    stored_at = base_store.get("stored_at") if isinstance(base_store, dict) else getattr(base_store, "stored_at", None)
    return stored_at if isinstance(stored_at, int) else None


async def clear_expired_base_store_reservations(matches: list[Match], *, bot: ipy.Client | None = None) -> bool:
    cutoff = latest_completed_raid_weekend_end_timestamp()
    reconciled = True
    for match in matches:
        for index, participant in enumerate(match.participants):
            stored_at = get_base_store_timestamp(participant)
            if stored_at is None or stored_at > cutoff:
                continue
            message_ids = await resolve_stored_base_message_ids(bot, match, index)
            if message_ids is None:
                continue
            for message_id in message_ids:
                await retire_stored_base_message(
                    bot, StoredBaseMessageRetirement(match.thread, message_id, "releasing"),
                )
            store = participant.base_store
            live_id = store.reservation_id if store and store.is_live else None
            if not await match.clear_base_store(index, reset_stored_flag=True):
                reconciled = False
                continue
            for message_id in message_ids:
                await retire_stored_base_message(
                    bot, StoredBaseMessageRetirement(match.thread, message_id, "raid_weekend_ended", live_id),
                )
    return reconciled


async def fetch_reconciled_base_store_matches(*, bot: ipy.Client | None = None) -> list[Match]:
    matches = await Match.find().to_list()
    if not await clear_expired_base_store_reservations(matches, bot=bot):
        return await Match.find().to_list()
    return matches


def has_unmapped_base_store_reservations(matches: list[Match]) -> bool:
    return any(
        is_unmapped_base_store_reservation(match, index)
        for match in matches
        if getattr(match, "thread", None)
        for index, _ in enumerate(getattr(match, "participants", []) or [])
    )


async def sync_base_store_slots(clan_tag: str | None = None, *, bot: ipy.Client | None = None) -> dict[str, list[str]]:
    matches = await fetch_reconciled_base_store_matches(bot=bot)
    occupied_slots = collect_occupied_base_store_slots(matches)
    blocks_all_slots = has_unmapped_base_store_reservations(matches)
    if clan_tag:
        clan = await Clan.find_one(Clan.tag == clan_tag)
        clans = [clan] if clan else []
    else:
        clans = await Clan.find_all().to_list()
    available_by_clan: dict[str, list[str]] = {}
    for clan in clans:
        available_slots = [] if blocks_all_slots else [
            slot for slot in BASE_STORE_SLOTS if slot not in occupied_slots.get(clan.tag, set())
        ]
        available_by_clan[clan.tag] = available_slots
        if list(clan.available_slots or []) != available_slots:
            clan.available_slots = available_slots
            await clan.save_changes()
    return available_by_clan


async def get_start_base_store_slot_availability(
        clan_tag: str,
        current_thread: str,
        *,
        bot: ipy.Client | None = None,
) -> tuple[list[str], dict[str, tuple[str, str]]]:
    matches = await fetch_reconciled_base_store_matches(bot=bot)
    foreign_matches = [
        match for match in matches if str(getattr(match, "thread", None)) != str(current_thread)
    ]
    slot_owners = collect_base_store_slot_owners(foreign_matches).get(clan_tag, {})
    return [slot for slot in BASE_STORE_SLOTS if slot not in slot_owners], slot_owners
