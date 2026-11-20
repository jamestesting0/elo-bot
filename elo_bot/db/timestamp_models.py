from datetime import UTC, datetime
from typing import Final

from beanie import Document
from pymongo import IndexModel


TIMESTAMP_DRAFT_INDEXES: Final = [IndexModel("expires_at", expireAfterSeconds=0, name="timestamp_draft_expiry")]
TIMESTAMP_POST_COLLECTION: Final = "timestamp_posts"
TIMESTAMP_POST_INDEXES: Final = [IndexModel("expires_at", expireAfterSeconds=0, name="timestamp_post_expiry")]


class TimestampPreference(Document):
    id: int
    zone: str

    class Settings:
        name = "timestamp_preferences"
        use_state_management = True


class TimestampDraft(Document):
    id: int
    owner_id: int
    channel_id: int
    expression: str
    anchor: int
    zone: str
    expires_at: datetime

    class Settings:
        name = "timestamp_drafts"
        use_state_management = True


class TimestampPost(Document):
    id: int
    user_id: int
    expires_at: datetime

    class Settings:
        name = TIMESTAMP_POST_COLLECTION

    @classmethod
    async def owner_for_message(cls, message_id: int) -> int | None:
        post = await cls.find_one({"_id": message_id, "expires_at": {"$gt": datetime.now(UTC)}})
        return post.user_id if post is not None else None
