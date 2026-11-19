from datetime import datetime
from typing import Final

from beanie import Document
from pymongo import IndexModel


TIMESTAMP_DRAFT_INDEXES: Final = [IndexModel("expires_at", expireAfterSeconds=0, name="timestamp_draft_expiry")]


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
