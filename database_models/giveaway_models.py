import secrets
from time import time
from typing import Annotated
from uuid import uuid4

from beanie import Document
from pydantic import Field, StringConstraints
from pymongo import ASCENDING, IndexModel, ReturnDocument


DiscordId = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]+$")]


GIVEAWAY_INDEXES = [
    IndexModel([("ended", ASCENDING), ("end_timestamp", ASCENDING)], name="giveaway_pending_end"),
    IndexModel([("end_timestamp", ASCENDING)], name="giveaway_end_timestamp"),
]


class Giveaway(Document):
    id: DiscordId = Field(alias="_id")
    end_timestamp: float = Field(ge=0)
    ended: bool = False
    winner_id: DiscordId | None = None
    draw_id: str | None = None
    entries: list[DiscordId] = Field(default_factory=list)
    sponsor_id: DiscordId
    min_elo: int = Field(default=0, ge=0)
    min_matches: int = Field(default=0, ge=0)
    channel_id: int = Field(gt=0)

    @classmethod
    async def add_entry(cls, message_id: str, user_id: str) -> "Giveaway | None":
        giveaway = await cls.get_pymongo_collection().find_one_and_update(
            {"_id": message_id, "entries": {"$ne": user_id}, "ended": {"$ne": True},
             "end_timestamp": {"$gt": time()}},
            {"$addToSet": {"entries": user_id}},
            return_document=ReturnDocument.AFTER,
        )
        if giveaway is None:
            return None
        return cls.model_validate(giveaway)

    async def claim_draw(self, *, reroll: bool = False) -> bool:
        if self.ended != reroll:
            return False
        winner = secrets.choice(self.entries) if self.entries else None
        draw_id = uuid4().hex
        updates = {"ended": True, "winner_id": winner, "draw_id": draw_id}
        result = await self.get_pymongo_collection().update_one(
            {"_id": self.id, "ended": self.ended, "entries": self.entries, "draw_id": self.draw_id},
            {"$set": updates},
        )
        if result.matched_count == 0:
            return False
        self.ended = True
        self.winner_id = winner
        self.draw_id = draw_id
        saved_state = self.get_saved_state()
        if saved_state is not None:
            saved_state.update(updates)
        return True

    class Settings:
        name = "giveaways"
        indexes = GIVEAWAY_INDEXES
        use_state_management = True


__all__ = ["DiscordId", "GIVEAWAY_INDEXES", "Giveaway"]
