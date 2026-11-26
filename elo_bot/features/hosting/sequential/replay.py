from datetime import datetime

from pymongo.asynchronous.client_session import AsyncClientSession

from elo_bot.config.game import BASE_STORE_SLOTS, REPLAY_CLAN_SUFFIX
from elo_bot.core.finals import FINAL_DISTRICTS, FinalError
from elo_bot.db import Clan, Match
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.features.base_store.start import held_store
from elo_bot.features.base_store.models import numeric_base_store_slot
from elo_bot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from elo_bot.features.base_store.repository import collect_occupied_base_store_slots, has_unmapped_base_store_reservations
from elo_bot.features.finals.matches import require_current_match, require_ongoing_match
from elo_bot.features.finals.lifecycle import ongoing_final
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.hosting.sequential.runs import match_runs, original_run, result_sides
from elo_bot.features.matches.resources import resource_clan_name, try_reserve_resource_clan


def replay_scores(match: Match, side: int) -> dict[str, list[str | None]]:
    return {district: [match.participants[player].result[index] for player in result_sides(side)]
            for index, district in enumerate(FINAL_DISTRICTS)}


def _check_stored_layouts(run: HelperSequentialRun, original: HelperSequentialRun, match: Match) -> None:
    payload = run.create_request["payload"]
    if run.create_request["type"] == "host_sequential_live_match_on_stored":
        stores = [store for player in (0, 1) if (store := held_store(match.participants[player])) is not None
                  and store.is_live and store.clan_tag == run.host_clan_tag and store.message_id is not None]
        if ([store.slot for store in stores] != ["Slot 1", "Slot 2"]
                or stores[0].reservation_id != stores[1].reservation_id):
            stores = []
    elif payload.get("base_source") == "stored":
        store = held_store(match.participants[run.side])
        stores = [store] if (store and store.message_id is not None and store.clan_tag == run.host_clan_tag
                             and store.slot == f"Slot {payload.get('slot')}") else []
    else:
        return
    if stores:
        ids = [store.reservation_id for store in stores]
        if original.layout_storage_ids is not None:
            if all(ids) and ids == original.layout_storage_ids:
                return
        elif all(store.stored_at is not None and store.stored_at <= original.created_at for store in stores):
            return
    raise FinalError("The original stored layouts are no longer available for Replay.")


async def _replay_create_request(run: HelperSequentialRun, clan_name: str, session: AsyncClientSession) -> dict:
    request = run.create_request
    payload = request["payload"]
    if request["type"] == "host_sequential_live_match_on_stored" or payload.get("base_source") == "stored":
        return request
    rows = await Match.get_pymongo_collection().find({"thread": {"$ne": None}, "$or": [
        {"participants.base_store.clan_tag": run.host_clan_tag}, {"participants.opponent_bases_stored": True},
    ]}, session=session).to_list()
    relevant = [Match.model_validate(row) for row in rows]
    occupied = collect_occupied_base_store_slots(relevant).get(run.host_clan_tag, set())
    slots = [slot for slot in BASE_STORE_SLOTS if slot not in occupied]
    if has_unmapped_base_store_reservations(relevant) or not slots or (run.side == LIVE_SIDE and len(slots) != 2):
        raise FinalError(f"**{clan_name}** does not have enough free base slots to replay these layouts.")
    if run.side == LIVE_SIDE:
        return request
    preferred = f"Slot {payload.get('slot')}"
    slot = preferred if preferred in slots else slots[0]
    return {**request, "payload": {**payload, "slot": numeric_base_store_slot(slot)}}


async def open_replay(run: HelperSequentialRun, original: HelperSequentialRun) -> None:
    async def claim(session: AsyncClientSession) -> None:
        matches = Match.get_pymongo_collection()
        runs = HelperSequentialRun.get_pymongo_collection()
        clans = Clan.get_pymongo_collection()
        match_row = await matches.find_one({"_id": run.match_id}, session=session)
        if match_row is None:
            raise FinalError("The match no longer exists.")
        match = Match.model_validate(match_row)
        final = await ongoing_final(run.final_id, session=session)
        node = require_current_match(final, match)
        if (match.final_contestant_revision, match.final_attempt) != (run.final_contestant_revision, run.final_attempt):
            raise FinalError("The Final pairing changed. Open its current Helper progress message.")
        if node.result_id or match.final_result_id or match.final_result_operation or match.final_replay_run_id:
            raise FinalError("This match already has a result or an active replay.")
        latest = await runs.find_one({"match_id": run.match_id, "side": run.side},
                                     sort=[("created_at", -1)], session=session)
        if latest is None or latest["_id"] != run.previous_run_id or latest["open"]:
            raise FinalError("A newer Helper run exists. Use its progress message.")
        if await runs.find_one({"match_id": run.match_id, "open": True}, session=session):
            raise FinalError("Wait for the open Helper run to finish before replaying districts.")
        start_side = 0 if run.side == LIVE_SIDE else run.side
        if match.host_clans[start_side] != run.host_clan_tag:
            raise FinalError("The host clan changed. Open the latest Helper progress message.")
        _check_stored_layouts(run, original, match)
        scores = replay_scores(match, run.side)
        assert run.replay_baseline is not None
        if any(scores[district] != run.replay_baseline[district] for district in run.districts):
            raise FinalError("Scores changed. Open **Replay** again.")

        clan = await clans.find_one({"tag": run.host_clan_tag}, session=session)
        if clan is None:
            raise FinalError("The original host clan is unavailable.")
        name = clan["name"]
        availability = clan["availability_str"]
        players = " vs ".join(f"<@{match.participants[side].user_id}>" for side in result_sides(run.side))
        owner = f"<#{match.thread}> {players} "
        if availability != "Available" and (not availability.startswith(owner) or availability.endswith(REPLAY_CLAN_SUFFIX)):
            raise FinalError(f"**{name}** is currently occupied.")
        request = await _replay_create_request(run, name, session)
        claim_text = f"{owner}<t:{int(datetime.now().timestamp())}:R>{REPLAY_CLAN_SUFFIX}"
        await clans.update_one({"_id": clan["_id"]}, {"$set": {"availability_str": claim_text}}, session=session)

        await matches.update_one({"_id": match.id}, {"$set": {
            "final_replay_run_id": run.id, "final_last_helper_run_id": run.id,
        }}, session=session)
        document = run.model_copy(update={"replay_baseline": scores, "replay_clan_claim": claim_text,
                                          "replay_previous_clan_availability": availability,
                                          "create_request": request})
        await runs.insert_one(document.model_dump(by_alias=True), session=session)

    async with BASE_STORE_RESERVATION_LOCK:
        reservation = await try_reserve_resource_clan(run.host_clan_tag)
        if reservation is None:
            name = await resource_clan_name(run.host_clan_tag)
            raise FinalError(f"**{name}** is currently occupied.")
        try:
            await transaction(claim)
        finally:
            reservation.release()


async def validate_replay_retry(run: HelperSequentialRun) -> None:
    match = await Match.get(run.match_id)
    if match is None:
        raise FinalError("The match no longer exists.")
    final = await require_ongoing_match(match)
    start_side = 0 if run.side == LIVE_SIDE else run.side
    if ((match.final_contestant_revision, match.final_attempt) != (run.final_contestant_revision, run.final_attempt)
            or final.nodes[match.final_node_id].result_id or match.final_replay_run_id != run.id
            or match.host_clans[start_side] != run.host_clan_tag):
        raise FinalError("This replay no longer matches the current pairing and host clan.")
    clan = await Clan.find_one({"tag": run.host_clan_tag})
    if clan is None or clan.availability_str != run.replay_clan_claim:
        raise FinalError("The replay's host clan reservation changed.")
    _check_stored_layouts(run, original_run(run, await match_runs(match.id, run.side)), match)
