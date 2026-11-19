from datetime import datetime
from typing import Literal

from beanie import Document, PydanticObjectId
from pymongo import IndexModel

from elo_bot.db.final_models import FinalResultSnapshot


class FinalResultDraft(Document):
    """A result preview whose state is consumed in the result transaction."""

    final_id: PydanticObjectId
    guild_id: int
    owner_id: str
    node_id: str
    expected_version: int
    contestant_revision: int
    attempt: int
    source_match_id: PydanticObjectId | None = None
    snapshot: FinalResultSnapshot
    explicit_winner: str | None = None
    reason: str | None = None
    expires_at: datetime
    state: Literal["PENDING", "CONFIRMED", "CANCELLED"] = "PENDING"
    result_id: str | None = None
    round_completed: bool = False

    class Settings:
        name = "final_result_drafts"
        use_state_management = True
        indexes = [IndexModel("expires_at", expireAfterSeconds=0, name="final_result_draft_expiry")]
