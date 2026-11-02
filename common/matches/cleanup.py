from beanie import PydanticObjectId
from pydantic import JsonValue

from database_models import Match


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
    result = await collection.delete_one(query)
    return result.deleted_count == 1 or await collection.find_one({"_id": match.id}, {"_id": 1}) is None
