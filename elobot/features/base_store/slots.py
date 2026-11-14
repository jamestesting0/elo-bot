from interactions import Client

from elobot.features.base_store.legacy import resolve_stored_base_message_ids
from elobot.features.base_store.messages import StoredBaseMessageRetirement, retire_stored_base_message
from elobot.config.game import BASE_STORE_SLOTS
from elobot.core.raid_weekend import latest_completed_raid_weekend_end_timestamp
from elobot.db import Clan, Match, Participant


def is_unmapped_base_store_reservation(match: Match, participant_index: int) -> bool:
    participant = match.participants[participant_index]
    if not getattr(participant, "opponent_bases_stored", False):
        return False
    if getattr(participant, "base_store", None):
        return False

    times = getattr(match, "times", None)
    return not isinstance(times, (list, tuple)) or participant_index >= len(times) or not times[participant_index]


def get_participant_base_store(participant: Participant) -> tuple[str | None, str | None]:
    base_store = getattr(participant, "base_store", None)
    if isinstance(base_store, dict):
        clan_tag = base_store.get("clan_tag")
        slot = base_store.get("slot")
    else:
        clan_tag = getattr(base_store, "clan_tag", None)
        slot = getattr(base_store, "slot", None)

    if not isinstance(clan_tag, str) or not isinstance(slot, str):
        return None, None

    return clan_tag, slot


def collect_base_store_slot_owners(matches: list[Match]) -> dict[str, dict[str, tuple[str, str]]]:
    slot_owners: dict[str, dict[str, tuple[str, str]]] = {}
    for match in matches:
        thread_id = getattr(match, "thread", None)
        if not thread_id:
            continue

        for participant in getattr(match, "participants", []) or []:
            clan_tag, slot = get_participant_base_store(participant)
            user_id = getattr(participant, "user_id", None)
            if clan_tag and slot in BASE_STORE_SLOTS and user_id:
                slot_owners.setdefault(clan_tag, {}).setdefault(slot, (str(thread_id), str(user_id)))

    return slot_owners


def format_base_store_slot_status(
        clan_tag: str,
        slot_owners: dict[str, dict[str, tuple[str, str]]],
) -> str:
    clan_slot_owners = slot_owners.get(clan_tag, {})
    lines = []
    for slot in BASE_STORE_SLOTS:
        owner = clan_slot_owners.get(slot)
        if not owner:
            continue

        thread_id, user_id = owner
        lines.append(f"{slot}: <#{thread_id}> <@{user_id}>")
    return "\n".join(lines)


def format_clan_availability_status(
        clan: Clan,
        slot_owners: dict[str, dict[str, tuple[str, str]]],
) -> str:
    slot_status = format_base_store_slot_status(clan.tag, slot_owners)
    return "\n".join(filter(None, (clan.availability_str, slot_status)))


async def clear_all_base_store_slots(*, bot: Client | None = None) -> None:
    cutoff = latest_completed_raid_weekend_end_timestamp()
    matches = await Match.find().to_list()
    for match in matches:
        for index, participant in enumerate(getattr(match, "participants", []) or []):
            store = participant.base_store
            if store is None and not is_unmapped_base_store_reservation(match, index):
                continue
            if store is not None and store.stored_at is not None and store.stored_at > cutoff:
                continue
            message_ids = await resolve_stored_base_message_ids(bot, match, index)
            if message_ids is None:
                continue
            for message_id in message_ids:
                await retire_stored_base_message(bot, StoredBaseMessageRetirement(match.thread, message_id, "releasing"))
            if await match.clear_base_store(index, reset_stored_flag=True):
                for message_id in message_ids:
                    await retire_stored_base_message(
                        bot, StoredBaseMessageRetirement(
                            match.thread, message_id, "raid_weekend_ended",
                            store.reservation_id if store and store.is_live else None,
                        ),
                    )

    remaining = await Match.find().to_list()
    occupied_slots = collect_base_store_slot_owners(remaining)
    blocks_all_slots = any(
        is_unmapped_base_store_reservation(match, index)
        for match in remaining if match.thread
        for index in range(len(match.participants))
    )
    clans = await Clan.find_all().to_list()
    for clan in clans:
        available_slots = [] if blocks_all_slots else [
            slot for slot in BASE_STORE_SLOTS if slot not in occupied_slots.get(clan.tag, {})
        ]
        if list(clan.available_slots or []) != available_slots:
            clan.available_slots = available_slots
            await clan.save_changes()
