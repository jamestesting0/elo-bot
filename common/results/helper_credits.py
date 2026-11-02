import anyio
from pydantic import JsonValue

from database_models import Helper, HelperHistory


_HELPER_CREDIT_LOCKS: dict[str, anyio.Lock] = {}


async def award_helper_once(
        user_id: str, receipt: str, points: float, history: HelperHistory | None = None,
) -> None:
    lock = _HELPER_CREDIT_LOCKS.setdefault(user_id, anyio.Lock())
    async with lock:
        collection = Helper.get_pymongo_collection()
        await collection.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"points": 0, "history": [], "accounts": [], "operation_receipts": []}},
            upsert=True,
        )
        update: dict[str, dict[str, JsonValue]] = {
            "$inc": {"points": points}, "$addToSet": {"operation_receipts": receipt},
        }
        if history is not None:
            update["$push"] = {"history": history.model_dump()}
        result = await collection.update_one(
            {"user_id": user_id, "operation_receipts": {"$ne": receipt}}, update,
        )
        if result.matched_count:
            return
        if await collection.find_one({"user_id": user_id, "operation_receipts": receipt}, projection={"_id": 1}):
            return
        raise LookupError(user_id)
