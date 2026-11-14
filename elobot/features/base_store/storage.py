from collections import Counter
import logging
from typing import assert_never

import interactions as ipy
from pymongo.errors import PyMongoError

from elobot.features.base_store.controls import resolve_stored_base_control, start_stored_bases, undo_stored_bases
from elobot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK, prepare_base_store
from elobot.features.base_store.repository import (
    clear_expired_base_store_reservations,
    collect_occupied_base_store_slots,
    fetch_reconciled_base_store_matches,
    get_base_store_timestamp,
    get_start_base_store_slot_availability,
    has_unmapped_base_store_reservations,
    sync_base_store_slots,
)
from elobot.features.base_store.messages import BaseStoreReleaseReason, StoredBaseMessageRetirement, retire_stored_base_message
from elobot.features.base_store.legacy import resolve_stored_base_message_ids
from elobot.features.results.validation import is_valid_complete_match_result
from elobot.features.finals.types import is_final_match
from elobot.core.raid_weekend import latest_completed_raid_weekend_end_timestamp
from elobot.db import Match
from elobot.features.base_store.self_service import store_self_bases


class BaseStoreReleaseConflict(RuntimeError):
    def __init__(self, token: str, player: int) -> None:
        self.token = token
        self.player = player
        super().__init__(f"Base storage release could not be confirmed for match {token}, participant {player}")


class BaseStoreReleaseDenied(RuntimeError):
    def __init__(self, token: str, player: int, reason: BaseStoreReleaseReason) -> None:
        self.token = token
        self.player = player
        self.reason = reason
        super().__init__(f"Base storage release is not allowed for match {token}, participant {player}: {reason}")


def _has_complete_recorded_results(match: Match) -> bool:
    if len(match.participants) < 2:
        return False
    if is_final_match(match):
        operation = match.final_result_operation
        return bool(operation and operation.completed and operation.helper_completed
                    and operation.result_id == match.final_result_id and operation.after.outcome == "Played"
                    and set(operation.after.contestants) == {participant.user_id for participant in match.participants}
                    and all(participant.result == operation.after.scores[participant.user_id]
                            and is_valid_complete_match_result("\n".join(participant.result), entry_count=7)
                            for participant in match.participants))
    operation = match.result_operation
    if operation is None or not operation.completed or operation.cancelled or operation.record is None:
        return False
    participant_ids = {participant.user_id for participant in match.participants[:2]}
    if (
            operation.record.operation_key != f"{match.id}:{operation.key}"
            or not participant_ids.issubset(operation.record.players)
    ):
        return False
    return all(
        len(participant.result) == 3
        and all(isinstance(result, str) for result in participant.result)
        and is_valid_complete_match_result("\n".join(participant.result))
        for participant in match.participants[:2]
    )


def _release_is_allowed(match: Match, index: int, reason: BaseStoreReleaseReason) -> bool:
    match reason:
        case "undone" | "thread_deleted":
            return True
        case "finished":
            return _has_complete_recorded_results(match)
        case "raid_weekend_ended":
            store = match.participants[index].base_store
            return store is None or store.stored_at is None or (
                store.stored_at <= latest_completed_raid_weekend_end_timestamp()
            )
        case unreachable:
            assert_never(unreachable)


async def clear_participant_base_store(
        match: Match,
        index: int,
        reset_stored_flag: bool = False,
        *,
        reason: BaseStoreReleaseReason,
        bot: ipy.Client | None = None,
        releasing_messages: set[int] | None = None,
        defer_final_messages: set[int] | None = None,
) -> bool:
    if not match or index < 0 or index >= len(match.participants):
        return False
    participant = match.participants[index]
    store = participant.base_store
    if store is None and not (reset_stored_flag and participant.opponent_bases_stored):
        return False
    if not _release_is_allowed(match, index, reason):
        raise BaseStoreReleaseDenied(match.token, index, reason)
    message_ids = () if reason == "thread_deleted" else await resolve_stored_base_message_ids(bot, match, index)
    if message_ids is None:
        return False
    for message_id in message_ids:
        if releasing_messages is not None and message_id in releasing_messages:
            continue
        await retire_stored_base_message(bot, StoredBaseMessageRetirement(match.thread, message_id, "releasing"))
        if releasing_messages is not None:
            releasing_messages.add(message_id)
    if not await match.clear_base_store(index, reset_stored_flag=reset_stored_flag):
        return False
    for message_id in message_ids:
        if defer_final_messages is not None and message_id in defer_final_messages:
            continue
        await retire_stored_base_message(bot, StoredBaseMessageRetirement(
            match.thread, message_id, reason, store.reservation_id if store and store.is_live else None,
        ))
    await sync_base_store_slots(store.clan_tag if store else None, bot=bot)
    return True


async def clear_match_base_stores(
        match: Match,
        reset_stored_flags: bool = False,
        *,
        reason: BaseStoreReleaseReason,
        bot: ipy.Client | None = None,
) -> None:
    if not match:
        return
    releasing_messages: set[int] = set()
    message_counts = Counter(
        participant.base_store.message_id
        for participant in match.participants
        if participant.base_store is not None and participant.base_store.message_id is not None
    )
    shared_live_messages = {
        store.message_id: StoredBaseMessageRetirement(match.thread, store.message_id, reason, store.reservation_id)
        for participant in match.participants
        if (store := participant.base_store) is not None and store.is_live
        and store.message_id is not None and message_counts[store.message_id] > 1
    }
    try:
        for index, participant in enumerate(match.participants):
            expected_storage = participant.base_store is not None or (
                reset_stored_flags and participant.opponent_bases_stored
            )
            cleared = await clear_participant_base_store(
                match, index, reset_stored_flags, bot=bot, reason=reason,
                releasing_messages=releasing_messages, defer_final_messages=set(shared_live_messages),
            )
            if expected_storage and not cleared:
                raise BaseStoreReleaseConflict(match.token, index)
    finally:
        for message_id, retirement in shared_live_messages.items():
            if message_id in releasing_messages:
                try:
                    await retire_stored_base_message(bot, retirement)
                except (ipy.errors.HTTPException, PyMongoError):
                    logging.exception(
                        "Unable to finalize shared base-storage message %s for match %s", message_id, match.token,
                    )
                    raise


__all__ = [
    "BASE_STORE_RESERVATION_LOCK",
    "BaseStoreReleaseConflict",
    "BaseStoreReleaseDenied",
    "clear_expired_base_store_reservations",
    "clear_match_base_stores",
    "clear_participant_base_store",
    "collect_occupied_base_store_slots",
    "fetch_reconciled_base_store_matches",
    "get_base_store_timestamp",
    "get_start_base_store_slot_availability",
    "has_unmapped_base_store_reservations",
    "prepare_base_store",
    "resolve_stored_base_control",
    "start_stored_bases",
    "store_self_bases",
    "sync_base_store_slots",
    "undo_stored_bases",
]
