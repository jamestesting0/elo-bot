from datetime import UTC, datetime

from elo_bot.db.timestamp_models import TimestampDraft, TimestampPreference


async def saved_zone(owner_id: int) -> str | None:
    preference = await TimestampPreference.get(owner_id)
    return preference.zone if preference is not None else None


async def remember_zone(owner_id: int, zone: str) -> None:
    await TimestampPreference.get_pymongo_collection().update_one({"_id": owner_id}, {"$set": {"zone": zone}}, upsert=True)


async def get_draft(draft_id: int, owner_id: int, channel_id: int) -> TimestampDraft | None:
    return await TimestampDraft.find_one({"_id": draft_id, "owner_id": owner_id, "channel_id": channel_id,
                                          "expires_at": {"$gt": datetime.now(UTC)}})


async def consume_draft(draft: TimestampDraft) -> bool:
    row = await TimestampDraft.get_pymongo_collection().find_one_and_delete({
        "_id": draft.id, "owner_id": draft.owner_id, "channel_id": draft.channel_id,
        "expires_at": {"$gt": datetime.now(UTC)},
    })
    return row is not None
