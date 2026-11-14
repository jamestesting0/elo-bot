from datetime import datetime
from secrets import token_hex

from cogs.base_store.models import BaseStorePreparationFailure, BaseStoreRuntime
from cogs.base_store.preparation import BASE_STORE_RESERVATION_LOCK
import cogs.live_hosting.helper_live as helper_live
from common.base_store.live import live_base_snapshot_filter
from elobot.config.game import BASE_STORE_SLOTS
from elobot.config.emojis import ERROR_EMOJI
from common.results.operations import result_input_error
from common.results.validation import is_match_finished
from elobot.db import BaseStore, Match


def live_storage_error(match: Match) -> str | None:
    if len(match.participants) != 2:
        return "This match does not have two participants."
    if any(is_match_finished(match, player=index) for index in (0, 1)) or result_input_error(match):
        return "This match has already finished or its results are being recorded."
    if any(match.times) or any(match.hosts):
        return "This match has already started."
    if any(p.base_store or p.opponent_bases_stored for p in match.participants):
        return "A stored-slot reservation already exists. Use its Undo control before storing again."
    return None


async def prepare_live_storage(
        runtime: BaseStoreRuntime, source: Match, clan_tag: str,
) -> Match | BaseStorePreparationFailure:
    async with BASE_STORE_RESERVATION_LOCK:
        match = await Match.find_one({"token": source.token, "thread": source.thread})
        if match is None:
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} This match is no longer available.")
        if reason := live_storage_error(match):
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} {reason}")
        if [p.user_id for p in match.participants] != [p.user_id for p in source.participants]:
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} The match participants changed. Storage was not started.")
        for index in (0, 1):
            await runtime._base_store_choose_bases(match, index)
        match = await Match.find_one({"token": source.token, "thread": source.thread})
        if match is None:
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} This match is no longer available.")
        if reason := live_storage_error(match):
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} {reason}")
        if [p.user_id for p in match.participants] != [p.user_id for p in source.participants]:
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} The match participants changed. Storage was not started.")
        missing = [
            f"<@{match.participants[1 - side].user_id}>: {match.final_districts[index]}"
            for side, participant in enumerate(match.participants)
            for index, base in enumerate(participant.bases) if base is None or not base.link
        ]
        if missing:
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} No eligible base could be selected for " + "; ".join(missing)
                + ". Have a helper check base availability or submit the missing bases, then retry. "
                  "Storage was not started."
            )
        if helper_live.parse_paired_live_bases(match) is None or len(match.final_districts) not in {3, 7}:
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} Both players need valid bases for every district. Storage was not started."
            )
        slots = (await runtime._base_store_sync_slots(clan_tag)).get(clan_tag, [])
        if not all(slot in slots for slot in BASE_STORE_SLOTS):
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} The selected helper clan no longer has both base slots free. Storage was not started."
            )
        reservation_id = token_hex(8)
        stores = [BaseStore(
            clan_tag=clan_tag, slot=slot, stored_at=int(datetime.now().timestamp()),
            reservation_id=reservation_id, ready=False, is_live=True,
        ) for slot in BASE_STORE_SLOTS]
        query = {
            "_id": match.id, "thread": source.thread, "times": list(match.times), "hosts": list(match.hosts),
            "is_autowin": {"$ne": True}, "autowin_winner_id": None, "result_operation": None,
            "final_districts": list(match.final_districts), **live_base_snapshot_filter(match),
        }
        updates = {}
        for index, participant in enumerate(match.participants):
            query.update({
                f"participants.{index}.user_id": participant.user_id,
                f"participants.{index}.result": list(participant.result),
                f"participants.{index}.base_store": None,
                f"participants.{index}.opponent_bases_stored": {"$ne": True},
            })
            updates[f"participants.{index}.base_store"] = stores[index].model_dump()
            updates[f"participants.{index}.opponent_bases_stored"] = True
        write = await match.get_pymongo_collection().update_one(query, {"$set": updates})
        if write.matched_count != 1:
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} The match changed while bases were being prepared. Storage was not started."
            )
        for index, store in enumerate(stores):
            match.participants[index].base_store = store
            match.participants[index].opponent_bases_stored = True
            match._sync_base_store_state(index)
        return match
