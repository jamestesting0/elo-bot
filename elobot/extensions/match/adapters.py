from typing import Literal

import interactions as ipy

import elobot.features.base_store.presentation as base_store_presentation
import elobot.features.base_store.storage as base_store_storage
from elobot.db import Match, Participant
from elobot.features.base_store.messages import BaseStoreReleaseReason

type ResultDetectionType = Literal["Strict", "Relaxed"]


def collect_occupied_base_store_slots(matches: list[Match]) -> dict[str, set[str]]:
    return base_store_storage.collect_occupied_base_store_slots(matches)


def get_base_store_timestamp(participant: Participant) -> int | None:
    return base_store_storage.get_base_store_timestamp(participant)


async def clear_expired_base_store_reservations(matches: list[Match], *, bot: ipy.Client | None = None) -> None:
    await base_store_storage.clear_expired_base_store_reservations(matches, bot=bot)


async def fetch_reconciled_base_store_matches(*, bot: ipy.Client | None = None) -> list[Match]:
    return await base_store_storage.fetch_reconciled_base_store_matches(bot=bot)


def has_unmapped_base_store_reservations(matches: list[Match]) -> bool:
    return base_store_storage.has_unmapped_base_store_reservations(matches)


async def sync_base_store_slots(clan_tag: str | None = None, *, bot: ipy.Client | None = None) -> dict[str, list[str]]:
    return await base_store_storage.sync_base_store_slots(clan_tag, bot=bot)


async def get_start_base_store_slot_availability(
        clan_tag: str,
        current_thread: str,
        *, bot: ipy.Client | None = None,
) -> tuple[list[str], dict[str, tuple[str, str]]]:
    return await base_store_storage.get_start_base_store_slot_availability(clan_tag, current_thread, bot=bot)


def construct_start_slot_instruction(available_slots: list[str], clan_name: str) -> str:
    return base_store_presentation.construct_start_slot_instruction(available_slots, clan_name)


def construct_occupied_slots_error(
        clan_tag: str,
        clan_name: str,
        slot_owners: dict[str, tuple[str, str]],
) -> str:
    return base_store_presentation.construct_occupied_slots_error(clan_tag, clan_name, slot_owners)


async def clear_participant_base_store(
        match: Match, index: int, reset_stored_flag: bool = False, *, bot: ipy.Client | None = None,
        reason: BaseStoreReleaseReason,
) -> bool:
    return await base_store_storage.clear_participant_base_store(
        match,
        index,
        reset_stored_flag=reset_stored_flag,
        bot=bot,
        reason=reason,
    )


async def clear_match_base_stores(
        match: Match, reset_stored_flags: bool = False, *, bot: ipy.Client | None = None,
        reason: BaseStoreReleaseReason,
) -> None:
    await base_store_storage.clear_match_base_stores(
        match, reset_stored_flags=reset_stored_flags, bot=bot, reason=reason,
    )
