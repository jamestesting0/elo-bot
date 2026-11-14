import asyncio  # noqa: ANYIO_OK - interactions.py runtime coordinates reservations with asyncio locks
import logging
from datetime import datetime
from secrets import token_hex

from elobot.config.game import BASE_STORE_SLOTS, HOST_CLAN_CHOICES
from elobot.config.emojis import ERROR_EMOJI
from elobot.core.match_results import is_match_finished
from elobot.features.results.operations import result_input_error
from elobot.features.base_store.models import (
    BaseStorePreparationFailure,
    BaseStorePreparationOutcome,
    BaseStorePreparationRequest,
    BaseStoreRuntime,
    PreparedBaseStore,
)
from elobot.db import BaseStore, Match
from elobot.features.base_store.raid_policy import eligible_for_raid_storage, select_raid_storage_slot


BASE_STORE_RESERVATION_LOCK = asyncio.Lock()


async def _claim_base_store(match: Match, player: int, store: BaseStore, *, raid_autostore: bool = False) -> bool:
    query = {
        "_id": match.id,
        "is_autowin": {"$ne": True},
        "autowin_winner_id": None,
        f"participants.{player}.base_store": None,
        f"participants.{player}.opponent_bases_stored": {"$ne": True},
        "result_operation": None,
    }
    for index in (0, 1) if raid_autostore or not match.is_war else (player,):
        query[f"times.{index}"] = match.times[index]
        query[f"hosts.{index}"] = match.hosts[index]
    if raid_autostore:
        opponent = 1 - player
        opponent_store = match.participants[opponent].base_store
        query.update({
            "thread": match.thread,
            "is_war": True,
            "recorded_match_id": None,
            f"participants.{player}.user_id": match.participants[player].user_id,
            f"participants.{opponent}.user_id": match.participants[opponent].user_id,
            f"participants.{opponent}.submit_bases": match.participants[opponent].submit_bases,
            f"participants.{opponent}.bases": [
                base.model_dump() if base else None for base in match.participants[opponent].bases
            ],
            f"participants.{opponent}.base_store": (
                opponent_store.model_dump() if opponent_store else None
            ),
        })
    for index, participant in enumerate(match.participants[:2]):
        query[f"participants.{index}.result"] = participant.result
    result = await match.get_pymongo_collection().update_one(
        query,
        {"$set": {
            f"participants.{player}.opponent_bases_stored": True,
            f"participants.{player}.base_store": store.model_dump(),
        }},
    )
    if result.matched_count == 0:
        return False
    match.participants[player].opponent_bases_stored = True
    match.participants[player].base_store = store
    match._sync_base_store_state(player)
    return True


async def prepare_base_store(
        runtime: BaseStoreRuntime,
        request: BaseStorePreparationRequest,
) -> BaseStorePreparationOutcome:
    async with BASE_STORE_RESERVATION_LOCK:
        match = await Match.find_one({"thread": request.thread_id})
        if not match or request.player >= len(match.participants):
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command "
                "in a 1v1 thread!"
            )
        if reason := result_input_error(match):
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} {reason}")
        if request.raid_autostore and not eligible_for_raid_storage(match, request.player):
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} This match is no longer eligible for raid storage.")
        participant = match.participants[request.player]
        player_id = participant.user_id
        finished_player = request.player if match.is_war else None
        if is_match_finished(match, player=finished_player):
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} The match has already finished for <@{player_id}>."
            )
        if participant.opponent_bases_stored or participant.base_store:
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} The bases for <@{player_id}> are already stored."
            )
        if request.clan is None:
            slot_map = await runtime._base_store_sync_slots()
            selection = select_raid_storage_slot(match, request.player, slot_map) if request.raid_autostore else next(
                (
                    (str(clan_choice.value), slot)
                    for slot in BASE_STORE_SLOTS
                    for clan_choice in HOST_CLAN_CHOICES
                    if slot in slot_map.get(str(clan_choice.value), [])
                ),
                None,
            )
            if selection is None:
                return BaseStorePreparationFailure(
                    f"{ERROR_EMOJI} No slot can be used while keeping free slots in two different clans."
                    if request.raid_autostore else f"{ERROR_EMOJI} There are no available configured base-store slots."
                )
            clan_tag, slot = selection
        else:
            clan_tag = request.clan.tag
            slot = request.slot
            if slot not in BASE_STORE_SLOTS:
                return BaseStorePreparationFailure(f"{ERROR_EMOJI} `{slot}` is not a valid base-store slot.")
            slot_map = await runtime._base_store_sync_slots(clan_tag)
            available_slots = slot_map.get(clan_tag)
            if available_slots is None:
                return BaseStorePreparationFailure(
                    f"{ERROR_EMOJI} Unable to verify base-store slots for **{request.clan.name}**."
                )
            if slot not in available_slots:
                return BaseStorePreparationFailure(
                    f"{ERROR_EMOJI} **{slot}** in **{request.clan.name}** is already being used for stored bases."
                )
        await runtime._base_store_choose_bases(match, request.player)
        match = await Match.find_one({"thread": request.thread_id})
        if not match or request.player >= len(match.participants):
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} The match changed while its bases were being prepared. Please try again."
            )
        if reason := result_input_error(match):
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} {reason}")
        if request.raid_autostore and not eligible_for_raid_storage(match, request.player):
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} This match is no longer eligible for raid storage.")
        participant = match.participants[request.player]
        finished_player = request.player if match.is_war else None
        if is_match_finished(match, player=finished_player):
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} The match has already finished for <@{participant.user_id}>."
            )
        if participant.opponent_bases_stored or participant.base_store:
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} The bases for <@{participant.user_id}> are already stored."
            )
        if request.raid_autostore and not all(match.participants[1 - request.player].bases):
            return BaseStorePreparationFailure(f"{ERROR_EMOJI} Not all bases could be selected. No slot was reserved.")
        if request.raid_autostore:
            selection = select_raid_storage_slot(match, request.player, await runtime._base_store_sync_slots())
            if selection is None:
                return BaseStorePreparationFailure(
                    f"{ERROR_EMOJI} No slot can be used while keeping free slots in two different clans."
                )
            clan_tag, slot = selection
            available_slots = [slot]
        else:
            available_slots = (await runtime._base_store_sync_slots(clan_tag)).get(clan_tag)
        if available_slots is None or slot not in available_slots:
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} **{slot}** is no longer available for stored bases. Please try again."
            )
        store = BaseStore(
            clan_tag=clan_tag,
            slot=slot,
            stored_at=int(datetime.now().timestamp()),
            reservation_id=token_hex(8),
            ready=False,
        )
        claimed = (
            await _claim_base_store(match, request.player, store, raid_autostore=True)
            if request.raid_autostore else await _claim_base_store(match, request.player, store)
        )
        if not claimed:
            return BaseStorePreparationFailure(
                f"{ERROR_EMOJI} The match or slot reservation changed while bases were being prepared. "
                "Please try again."
            )
        reservation_id = store.reservation_id
        try:
            await runtime._base_store_sync_slots(clan_tag)
        except Exception:  # noqa: BROAD_EXCEPT_OK - reservation remains durable and the caller must publish Undo
            logging.exception(
                "Unable to update base-store slot projection for match %s, participant %s",
                match.token,
                request.player,
            )
    prepared_match = await Match.find_one({"thread": request.thread_id})
    if not prepared_match:
        return BaseStorePreparationFailure(
            f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!"
        )
    current_store = prepared_match.participants[request.player].base_store
    if current_store is None or current_store.reservation_id != reservation_id:
        return BaseStorePreparationFailure(
            f"{ERROR_EMOJI} The base-store reservation could not be confirmed. Please try again or contact a helper."
        )
    return PreparedBaseStore(
        match=prepared_match,
        player=request.player,
        clan_tag=clan_tag,
        slot=slot,
    )
