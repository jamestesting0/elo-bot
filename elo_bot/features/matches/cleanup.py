from beanie import PydanticObjectId
from pydantic import JsonValue

from elo_bot.db import Match
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.tokens.challenge_items import return_challenge_code
from elo_bot.features.tokens.queue_items import finish_district_items
from elo_bot.features.tokens.ranked import release_protections
from elo_bot.features.tokens.stakes import reconcile_stake


def has_pending_match_operation(match: Match) -> bool:
    final_operation = getattr(match, "final_result_operation", None)
    if final_operation is not None and not final_operation.published:
        return True
    operation = match.result_operation
    return bool(
        operation and not operation.published and not operation.cancelled
        or match.autowin_winner_id is not None and not match.autowin_completed
    )


def has_reserved_match_slots(match: Match) -> bool:
    return any(participant.base_store is not None or participant.opponent_bases_stored
               for participant in match.participants)


async def delete_unreserved_match(
        match: Match, *, pre_thread: bool = False, allow_pending_operations: bool = False,
) -> bool:
    """Delete only the captured thread and confirmations when no stored bases remain."""
    if pre_thread and (match.thread is not None or all(participant.confirmed for participant in match.participants)):
        return False
    query: dict[str, JsonValue | PydanticObjectId] = {
        "_id": match.id,
        "thread": match.thread,
        "participants": {"$not": {"$elemMatch": {"$or": [
            {"base_store": {"$ne": None}}, {"opponent_bases_stored": True},
        ]}}},
    }
    for index, participant in enumerate(match.participants):
        query[f"participants.{index}.user_id"] = participant.user_id
        query[f"participants.{index}.confirmed"] = participant.confirmed
    if not allow_pending_operations:
        query["$and"] = [
            {"$or": [{"final_result_operation": None}, {"final_result_operation.published": True}]},
            {"$or": [
                {"result_operation": None},
                {"result_operation.published": True},
                {"result_operation.cancelled": True},
            ]},
            {"$or": [{"autowin_winner_id": None}, {"autowin_completed": True}]},
        ]
    collection = match.get_pymongo_collection()
    if match.match_stake_enabled is True or match.token_challenge_use or match.token_districts or match.token_protections:
        async def delete_with_tokens(session):
            row = await collection.find_one_and_delete(query, session=session)
            if row is None:
                return await collection.find_one({"_id": match.id}, {"_id": 1}, session=session) is None
            captured = Match.model_validate(row)
            if captured.match_stake_enabled:
                await reconcile_stake(str(captured.id), deleted=captured, session=session)
            await return_challenge_code(captured, session)
            await finish_district_items(captured, consume=captured.thread is not None, session=session)
            await release_protections(captured, session)
            return True

        return await transaction(delete_with_tokens)
    result = await collection.delete_one(query)
    return result.deleted_count == 1 or await collection.find_one({"_id": match.id}, {"_id": 1}) is None
