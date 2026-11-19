import time

from beanie import PydanticObjectId
from pymongo.asynchronous.client_session import AsyncClientSession

from elo_bot.db.final_invitation_models import FinalInvitation


async def expire_invitations(*, session: AsyncClientSession | None = None) -> None:
    await FinalInvitation.get_pymongo_collection().update_many(
        {"state": "pending", "expires_at": {"$lte": int(time.time())}},
        {"$set": {"state": "expired", "dm_outdated": True}}, session=session)


async def withdraw_pending(final_id: PydanticObjectId, session: AsyncClientSession | None = None,
                           *, user_id: str | None = None) -> None:
    query: dict = {"final_id": final_id, "state": "pending"}
    if user_id is not None:
        query["user_id"] = user_id
    await FinalInvitation.get_pymongo_collection().update_many(
        query, {"$set": {"state": "withdrawn", "dm_outdated": True}}, session=session)
